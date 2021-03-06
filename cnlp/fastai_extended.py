import random

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset
import numpy as np

import fastai.text
from fastai.core import BasicModel, to_gpu
from fastai.nlp import RNN_Learner
from fastai.lm_rnn import SequentialRNN
from fastai.dataloader import DataLoader

from .transformer_decoder import TransformerEncoder, LayerNorm


class TransformerLearner(RNN_Learner):
    def fit(self, *args, **kwargs):
        return super().fit(*args, **kwargs, seq_first=False)


class LanguageModelLoader:
    """ Returns a language model iterator that iterates through batches that are of length N(bptt,5)
    The first batch returned is always bptt+25; the max possible width.  This is done because of they way that pytorch
    allocates cuda memory in order to prevent multiple buffers from being created as the batch width grows.
    """

    MAX_PLUS = 25

    def __init__(self,
                 nums: np.array,
                 bs: int,
                 bptt: int,
                 target_length: int,
                 backwards: bool = False,
                 batch_first: bool = False,
                 randomize_bptt: bool = False):
        self.bs, self.bptt, self.backwards = bs, bptt, backwards
        self.batch_first = batch_first
        self.data = self.batchify(nums)
        self.i, self.iter = 0, 0
        self.n = self.data.size(1) if self.batch_first else self.data.size(0)
        self.randomize_bptt = randomize_bptt
        self.target_length = target_length

    @property
    def max_possible_seq_len(self) -> int:
        if self.randomize_bptt is False:
            return self.bptt
        return self.bptt + self.MAX_PLUS

    def __iter__(self):
        self.i, self.iter = 0, 0
        while self.i < self.n - 1 and self.iter < len(self):
            if self.randomize_bptt:
                if self.i == 0:
                    seq_len = self.bptt + 5 * 5
                else:
                    bptt = self.bptt if np.random.random(
                    ) < 0.95 else self.bptt / 2.
                    seq_len = max(
                        5,
                        min(
                            int(np.random.normal(bptt, 5)),
                            self.max_possible_seq_len))
            else:
                seq_len = self.bptt
            if self.i + seq_len >= self.n:
                # ditch residuals
                break
            res = self.get_batch(self.i, seq_len)
            self.i += seq_len
            self.iter += 1
            yield res

    def __len__(self):
        return self.n // self.bptt - 1

    def batchify(self, data):
        nb = data.shape[0] // self.bs
        data = np.array(data[:nb * self.bs])
        data = data.reshape(self.bs, -1)
        if self.backwards:
            data = data[:, ::-1]
        if not self.batch_first:
            data = data.T
        return torch.from_numpy(data.astype("int64"))

    def get_batch(self, i, seq_len):
        source = self.data
        target_offset = max(0, seq_len - self.target_length)
        if self.batch_first:
            return (source[:, i:(i + seq_len)].contiguous(),
                    source[:, (i + 1 + target_offset):(
                        i + 1 + seq_len)].contiguous().view(-1))
        else:
            return (source[i:(i + seq_len)].contiguous(),
                    source[(i + 1 + target_offset):(
                        i + 1 + seq_len)].contiguous().view(-1))


class ShuffledLanguageModelLoader(LanguageModelLoader):
    """An alternative algorithm to LanguageModelLoader

    Useful for models that do not pass information between batches.
    """

    def __init__(self,
                 nums: np.array,
                 bs: int,
                 bptt: int,
                 target_length: int,
                 batch_first: bool = False,
                 randomize_bptt: bool = False):
        # We intentional don't invoke super class's initializer
        # as we only want to reuse batchify and get_batch method
        super().__init__(nums, bs, bptt, target_length, False, batch_first,
                         randomize_bptt)

    def __iter__(self):
        for step in range(len(self)):
            i = random.randint(0, self.n - self.max_possible_seq_len - 1)
            if self.randomize_bptt:
                if step == 0:
                    seq_len = self.bptt + 5 * 5
                else:
                    bptt = self.bptt if np.random.random(
                    ) < 0.95 else self.bptt / 2.
                    seq_len = max(
                        5,
                        min(
                            int(np.random.normal(bptt, 5)),
                            self.max_possible_seq_len))
            else:
                seq_len = self.bptt
            res = self.get_batch(i, seq_len)
            yield res


class TextDataset(Dataset):
    def __init__(self,
                 x,
                 y,
                 backwards=False,
                 sos=None,
                 eos=None,
                 max_seq_len=-1,
                 cut_tail=True):
        self.x, self.y, self.backwards, self.sos, self.eos = x, y, backwards, sos, eos
        self.max_seq_len = max_seq_len
        self.cut_tail = cut_tail

    def __getitem__(self, idx):
        x = self.x[idx]
        if self.max_seq_len > 0:
            if self.cut_tail:
                x = x[:self.max_seq_len]
            else:
                x = x[-self.max_seq_len:]
        if self.backwards: x = list(reversed(x))
        if self.eos is not None: x = x + [self.eos]
        if self.sos is not None: x = [self.sos] + x
        return np.array(x), self.y[idx]

    def __len__(self):
        return len(self.x)


class FixedLengthDataLoader(DataLoader):
    def __init__(self,
                 dataset,
                 seq_length,
                 batch_size=1,
                 shuffle=False,
                 sampler=None,
                 batch_sampler=None,
                 pad_idx=0,
                 num_workers=None,
                 pin_memory=False,
                 drop_last=False,
                 pre_pad=True,
                 half=False,
                 transpose=False,
                 transpose_y=False):
        super().__init__(
            dataset,
            batch_size=batch_size,
            shuffle=shuffle,
            sampler=sampler,
            batch_sampler=batch_sampler,
            pad_idx=pad_idx,
            num_workers=num_workers,
            pin_memory=pin_memory,
            drop_last=drop_last,
            pre_pad=pre_pad,
            half=half,
            transpose=transpose,
            transpose_y=transpose_y)
        self.seq_length = seq_length

    def jag_stack(self, b):
        if len(b[0].shape) not in (1, 2): return np.stack(b)
        ml = self.seq_length
        if min(len(o) for o in b) == ml: return np.stack(b)
        res = np.zeros((len(b), ml), dtype=b[0].dtype) + self.pad_idx
        for i, o in enumerate(b):
            if self.pre_pad: res[i, -len(o):] = o
            else: res[i, :len(o)] = o
        return res


class TransformerLanguageModel(BasicModel):
    def get_layer_groups(self):
        enc = self.model[0]
        dec = self.model[1]
        return [enc.embed, *enc.blocks, dec]


class LanguageModelData(fastai.text.LanguageModelData):
    def get_transformer_model(self, opt_fn, emb_sz, max_seq_len, **kwargs):
        m = get_transformer_language_model(
            self.n_tok,
            max_seq_len,
            self.trn_dl.target_length,
            emb_sz,
            pad_token=self.pad_idx,
            **kwargs)
        model = TransformerLanguageModel(to_gpu(m))
        return TransformerLearner(self, model, opt_fn=opt_fn)


class FlattenPredictions(nn.Module):
    def __init__(self, target_len: int):
        super().__init__()
        self.target_len = target_len

    def forward(self, x):
        return x[:, -self.target_len:, :].contiguous().view(-1, x.size(2))


def get_transformer_language_model(n_tok: int,
                                   max_seq_len: int,
                                   target_length: int,
                                   emb_sz: int,
                                   n_head: int,
                                   n_layer: int,
                                   pad_token: int,
                                   embd_pdrop: float = 0.1,
                                   attn_pdrop: float = 0.1,
                                   resid_pdrop: float = 0.1,
                                   afn: str = 'gelu'):
    enc = TransformerEncoder(
        vocab=n_tok,
        n_ctx=max_seq_len,
        n_embd=emb_sz,
        n_head=n_head,
        n_layer=n_layer,
        pad_token=pad_token,
        embd_pdrop=embd_pdrop,
        attn_pdrop=attn_pdrop,
        resid_pdrop=resid_pdrop,
        afn=afn)
    decoder = nn.Linear(emb_sz, n_tok, bias=False)
    decoder.weight = nn.Parameter(
        enc.embed.weight[:-max_seq_len])  # Tied weights
    return SequentialRNN(enc, decoder, FlattenPredictions(target_length))


class LinearBlock(nn.Module):
    def __init__(self, ni, nf, drop, norm=True):
        super().__init__()
        self.lin = nn.Linear(ni, nf)
        self.drop = nn.Dropout(drop)
        self.norm = norm
        if norm:
            self.ln = nn.LayerNorm(ni)
            # self.ln = nn.BatchNorm1d(ni)
        nn.init.kaiming_normal_(self.lin.weight)
        nn.init.constant_(self.lin.bias, 0)

    def forward(self, x):
        if self.norm:
            # return self.ln(self.lin(self.drop(x)))
            return self.lin(self.drop(self.ln(x)))
        else:
            return self.lin(self.drop(x))


class PoolingLinearClassifier(nn.Module):
    def __init__(self, layers, drops, batch_first=False):
        super().__init__()
        self.batch_first = batch_first
        self.layers = nn.ModuleList([
            LinearBlock(
                layers[i],
                layers[i + 1],
                drops[i],
                norm=(i != len(layers) - 2)) for i in range(len(layers) - 1)
        ])

    def pool(self, x, bs, is_max):
        f = F.adaptive_max_pool1d if is_max else F.adaptive_avg_pool1d
        if self.batch_first:
            return f(x.permute(0, 2, 1), (1, )).squeeze(-1)
        return f(x.permute(1, 2, 0), (1, )).view(bs, -1)

    def forward(self, output):
        if self.batch_first:
            sl, bs, _ = output.size()
        else:
            bs, sl, _ = output.size()
        avgpool = self.pool(output, bs, False)
        mxpool = self.pool(output, bs, True)
        if self.batch_first:
            x = torch.cat([output[:, -1, :], mxpool, avgpool], 1)
        else:
            x = torch.cat([output[-1], mxpool, avgpool], 1)
        for l in self.layers:
            l_x = l(x)
            x = F.relu(l_x)
        return l_x


class MLP(nn.Module):
    def __init__(self, layers, drops, batch_first=False):
        super().__init__()
        self.batch_first = batch_first
        self.layers = nn.ModuleList([
            LinearBlock(layers[i], layers[i + 1], drops[i], norm=True)
            # norm=(i != len(layers) - 2))
            for i in range(len(layers) - 1)
        ])

    def forward(self, output):
        if self.batch_first:
            x = output[:, -1, :]
        else:
            x = output[-1]
        for l in self.layers:
            l_x = l(x)
            x = F.relu(l_x)
        return l_x


# class TruncateSequence(nn.Module):
#     def __init__(self, max_seq_len: int):
#         super().__init__()
#         self.max_seq_len = max_seq_len

#     def forward(self, x):
#         # Use the end of the sequences
#         return x[:, -self.max_seq_len:]

# class TruncatedTransformerLearner(RNN_Learner):
#     def save_encoder(self, name):
#         save_model(self.model[0], self.get_model_path(name))

#     def load_encoder(self, name):
#         load_model(self.model[0], self.get_model_path(name))


def get_transformer_classifier(n_tok: int,
                               n_ctx: int,
                               emb_sz: int,
                               n_head: int,
                               n_layer: int,
                               clf_layers: int,
                               pad_token: int,
                               embd_pdrop: float = 0.1,
                               attn_pdrop: float = 0.1,
                               resid_pdrop: float = 0.1,
                               clf_pdrop: float = 0.1,
                               afn: str = 'gelu'):
    enc = TransformerEncoder(
        vocab=n_tok,
        n_ctx=n_ctx,
        n_embd=emb_sz,
        n_head=n_head,
        n_layer=n_layer,
        pad_token=pad_token,
        embd_pdrop=embd_pdrop,
        attn_pdrop=attn_pdrop,
        resid_pdrop=resid_pdrop,
        afn=afn)
    classifier = MLP(clf_layers, clf_pdrop, batch_first=True)
    return SequentialRNN(enc, classifier)


class TransformerTextModel(BasicModel):
    def get_layer_groups(self):
        enc = self.model[0]
        clf = self.model[1]
        return [enc.embed, *enc.blocks, clf]
