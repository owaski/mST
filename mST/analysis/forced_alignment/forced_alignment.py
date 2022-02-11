import os
from dataclasses import dataclass

import IPython
import matplotlib
import matplotlib.pyplot as plt
import requests
import torch
import torchaudio
from tqdm import tqdm
import pandas as pd

import re
import soundfile
from examples.speech_to_text.data_utils import load_df_from_tsv

torch.random.manual_seed(0)
device = torch.device("cuda")

bundle = torchaudio.pipelines.WAV2VEC2_ASR_LARGE_LV60K_960H
model = bundle.get_model().to(device)
labels = bundle.get_labels()
dictionary = {c: i for i, c in enumerate(labels)}

def get_trellis(emission, tokens, blank_id=0):
    num_frame = emission.size(0)
    num_tokens = len(tokens)

    # Trellis has extra diemsions for both time axis and tokens.
    # The extra dim for tokens represents <SoS> (start-of-sentence)
    # The extra dim for time axis is for simplification of the code.
    trellis = torch.full((num_frame + 1, num_tokens + 1), -float("inf"))
    trellis[:, 0] = 0
    for t in range(num_frame):
        trellis[t + 1, 1:] = torch.maximum(
            # Score for staying at the same token
            trellis[t, 1:] + emission[t, blank_id],
            # Score for changing to the next token
            trellis[t, :-1] + emission[t, tokens],
        )
    return trellis

@dataclass
class Point:
    token_index: int
    time_index: int
    score: float


def backtrack(trellis, emission, tokens, blank_id=0):
    # Note:
    # j and t are indices for trellis, which has extra dimensions
    # for time and tokens at the beginning.
    # When referring to time frame index `T` in trellis,
    # the corresponding index in emission is `T-1`.
    # Similarly, when referring to token index `J` in trellis,
    # the corresponding index in transcript is `J-1`.
    j = trellis.size(1) - 1
    t_start = torch.argmax(trellis[:, j]).item()

    path = []
    for t in range(t_start, 0, -1):
        # 1. Figure out if the current position was stay or change
        # Note (again):
        # `emission[J-1]` is the emission at time frame `J` of trellis dimension.
        # Score for token staying the same from time frame J-1 to T.
        stayed = trellis[t - 1, j] + emission[t - 1, blank_id]
        # Score for token changing from C-1 at T-1 to J at T.
        changed = trellis[t - 1, j - 1] + emission[t - 1, tokens[j - 1]]

        # 2. Store the path with frame-wise probability.
        prob = emission[t - 1, tokens[j - 1] if changed > stayed else 0].exp().item()
        # Return token index and time index in non-trellis coordinate.
        path.append(Point(j - 1, t - 1, prob))

        # 3. Update the token
        if changed > stayed:
            j -= 1
            if j == 0:
                break
    else:
        raise ValueError("Failed to align")
    return path[::-1]


@dataclass
class Segment:
    label: str
    start: int
    end: int
    score: float

    def __repr__(self):
        return f"{self.label}\t({self.score:4.2f}): [{self.start:5d}, {self.end:5d})"

    @property
    def length(self):
        return self.end - self.start


def merge_repeats(path):
    i1, i2 = 0, 0
    segments = []
    while i1 < len(path):
        while i2 < len(path) and path[i1].token_index == path[i2].token_index:
            i2 += 1
        score = sum(path[k].score for k in range(i1, i2)) / (i2 - i1)
        segments.append(
            Segment(
                transcript[path[i1].token_index],
                path[i1].time_index,
                path[i2 - 1].time_index + 1,
                score,
            )
        )
        i1 = i2
    return segments

def merge_words(segments, separator="|"):
    words = []
    i1, i2 = 0, 0
    while i1 < len(segments):
        if i2 >= len(segments) or segments[i2].label == separator:
            if i1 != i2:
                segs = segments[i1:i2]
                word = "".join([seg.label for seg in segs])
                score = sum(seg.score * seg.length for seg in segs) / sum(seg.length for seg in segs)
                words.append(Segment(word, segments[i1].start, segments[i2 - 1].end, score))
            i1 = i2 + 1
            i2 = i1
        else:
            i2 += 1
    return words


mustc_root = '/mnt/raid0/siqi/datasets/must-c'
train_df = load_df_from_tsv(os.path.join(mustc_root, 'en-de/train_wave.tsv'))
noises = ['(Applause)', '(Audience)', '(Audio)', '(Beat)', '(Beatboxing)', '(Beep)', '(Beeps)', '(Cheering)', '(Cheers)', '(Claps)', '(Clicking)', '(Clunk)', '(Coughs)', \
    '(Drums)', '(Explosion)', '(Gasps)', '(Guitar)', '(Honk)', '(Laugher)', '(Laughing)', '(Laughs)', '(Laughter)', '(Mumbling)', '(Music)', '(Noise)', '(Recording)', \
    '(Ringing)', '(Shouts)', '(Sigh)', '(Sighs)', '(Silence)', '(Singing)', '(Sings)', '(Spanish)', '(Static)', '(Tones)', '(Trumpet)', '(Video)', '(Voice-over)', \
    '(Whistle)', '(Whistling)', '(video)']

def clean(text):
    prefix = re.match("(.{,20}:).*", text)
    if prefix is not None:
        text = text[len(prefix.group(1)):]
    for noise in noises:
        text = text.replace(noise, '')
    tokens = []
    for c in text:
        if c.isalpha():
            if c.upper() in dictionary:
                tokens.append(c.upper())
        elif c == "'":
            tokens.append(c)
        else:
            tokens.append('|')
    transcript = []
    for c in tokens:
        if c == '|':
            if len(transcript) > 0 and transcript[-1] != '|':
                transcript.append(c)
        else:
            transcript.append(c)
    if len(transcript) > 0 and transcript[-1] == '|':
        transcript.pop()
    
    return ''.join(transcript)

mismatch_df = pd.DataFrame(columns=train_df.columns)
iterator = tqdm(train_df.iterrows(), total=train_df.shape[0], desc='0 mismatch found')
for _, row in iterator:
    try:
        splits = row['audio'].split(':')
        ori_start, ori_duration = splits[-2:]
        ori_start, ori_duration = int(ori_start), int(ori_duration)
        
        wav_file = os.path.join(mustc_root, ''.join(splits[:-2]))
        with torch.inference_mode():
            waveform, _ = torchaudio.load(wav_file, \
                frame_offset=max(ori_start - 3 * 16000, 0), num_frames=ori_duration + 6 * 16000)
            emissions, _ = model(waveform.to(device))
            emissions = torch.log_softmax(emissions, dim=-1)
        emission = emissions[0].cpu().detach()

        transcript = clean(row['src_text'])
        if transcript == '':
            continue
        tokens = [dictionary[c] for c in transcript]
        trellis = get_trellis(emission, tokens)
        path = backtrack(trellis, emission, tokens)
        segments = merge_repeats(path)
        word_segments = merge_words(segments)

        ratio = waveform.size(1) / (trellis.size(0) - 1)
        start = ratio * word_segments[0].start
        end = ratio * word_segments[-1].end

        if start < 2.75 * 16000 or end > waveform.size(1) - 2.75 * 16000:
            mismatch_df = mismatch_df.append(row, ignore_index=True)
            iterator.set_description('{} mismatch found'.format(mismatch_df.shape[0]))
    except:
        pass

string = '<table>\n'
string += '\t<tr>\n\t\t<th>Transcript</th>\n\t\t<th>Translation</th>\n\t\t<th>Source Audio</th>\n\t</tr>\n'
for i in tqdm(range(mismatch_df.shape[0])):
    match = re.match('(.*):(\d+):(\d+)', mismatch_df['audio'][i])
    wav = os.path.join(mustc_root, match.group(1))
    speech_array, sampling_rate = soundfile.read( wav )
    speech_array = speech_array[int(match.group(2)):int(match.group(2))+int(match.group(3))]
    soundfile.write('mST/analysis/forced_alignment/resources/train-mismatch/audio_{}.wav'.format(i), speech_array, 16000)
    string += '\t<tr>\n\t\t<td>{}</td>\n\t\t<td>{}</td>\n\t\t<td>{}</td>\n\t</tr>\n'.format(\
        mismatch_df['src_text'][i], mismatch_df['tgt_text'][i], \
        '<audio controls><source src="train-mismatch/audio_{}.wav" type="audio/wav"></audio>'.format(i))
string += '</table>'

with open('mST/analysis/forced_alignment/train-mismatch.html', 'w') as w:
    w.write(string)