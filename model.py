# Copyright (c) 2020 DeNA Co., Ltd.
# Licensed under The MIT License [see LICENSE for details]

# neural nets

import numpy as np
import torch
torch.set_num_threads(1)

import torch.nn as nn
import torch.nn.functional as F


def to_torch(x, transpose=False):
    if x is None:
        return None
    elif isinstance(x, torch.Tensor):
        return x

    a = np.array(x)
    if transpose:
        a = np.swapaxes(a, 0, 1)

    if a.dtype == np.int32 or a.dtype == np.int64:
        t = torch.LongTensor(a)
    else:
        t = torch.FloatTensor(a)

    return t.contiguous()


def to_numpy(x):
    if x is None:
        return None
    elif isinstance(x, torch.Tensor):
        a = x.detach().numpy()
    elif isinstance(x, np.ndarray):
        a = x
    elif isinstance(x, tuple):
        return tuple(to_numpy(xx) for xx in x)
    else:
        a = np.array(x)
    return a


def to_gpu(data):
    if data is None:
        return None
    if isinstance(data, tuple):
        return tuple(to_gpu(d) for d in data)
    elif isinstance(data, list):
        return [to_gpu(d) for d in data]
    elif isinstance(data, dict):
        return {k: to_gpu(d) for k, d in data.items()}
    return data.cuda()


def to_gpu_or_not(data, gpu):
    return to_gpu(data) if gpu else data


def softmax(x):
    x = np.exp(x - np.max(x, axis=-1))
    return x / x.sum(axis=-1)


class Conv(nn.Module):
    def __init__(self, filters0, filters1, kernel_size, bn, bias=True):
        super().__init__()
        if bn:
            bias = False
        self.conv = nn.Conv2d(
            filters0, filters1, kernel_size,
            stride=1, padding=kernel_size//2, bias=bias
        )
        self.bn = nn.BatchNorm2d(filters1) if bn else None

    def forward(self, x):
        h = self.conv(x)
        if self.bn is not None:
            h = self.bn(h)
        return h


class Dense(nn.Module):
    def __init__(self, units0, units1, bnunits=0, bias=True):
        super().__init__()
        if bnunits > 0:
            bias = False
        self.dense = nn.Linear(units0, units1, bias=bias)
        self.bn = nn.BatchNorm1d(bnunits) if bnunits > 0 else None

    def forward(self, x):
        h = self.dense(x)
        if self.bn is not None:
            size = h.size()
            h = h.view(h.size(0), -1)
            h = self.bn(h)
            h = h.view(*size)
        return h


class WideResidualBlock(nn.Module):
    def __init__(self, filters, kernel_size, bn):
        super().__init__()
        self.conv1 = Conv(filters, filters, kernel_size, bn, not bn)
        self.conv2 = Conv(filters, filters, kernel_size, bn, not bn)

    def forward(self, x):
        return F.relu(x + self.conv2(F.relu(self.conv1(x))))


class WideResNet(nn.Module):
    def __init__(self, blocks, filters):
        super().__init__()
        self.blocks = nn.ModuleList([
            WideResidualBlock(filters, 3, bn=False) for _ in range(blocks)
        ])

    def forward(self, x):
        h = x
        for block in self.blocks:
            h = block(h)
        return h


class Encoder(nn.Module):
    def __init__(self, input_size, filters):
        super().__init__()

        self.input_size = input_size
        self.conv = Conv(input_size[0], filters, 3, bn=False)
        self.activation = nn.LeakyReLU(0.1)

    def forward(self, x):
        return self.activation(self.conv(x))


class Head(nn.Module):
    def __init__(self, input_size, out_filters, outputs):
        super().__init__()

        self.board_size = input_size[1] * input_size[2]
        self.out_filters = out_filters

        self.conv = Conv(input_size[0], out_filters, 1, bn=False)
        self.activation = nn.LeakyReLU(0.1)
        self.fc = nn.Linear(self.board_size * out_filters, outputs, bias=False)

    def forward(self, x):
        h = self.activation(self.conv(x))
        h = self.fc(h.view(-1, self.board_size * self.out_filters))
        return h


class ConvLSTMCell(nn.Module):
    def __init__(self, input_dim, hidden_dim, kernel_size, bias):
        super().__init__()

        self.input_dim = input_dim
        self.hidden_dim = hidden_dim

        self.kernel_size = kernel_size
        self.padding = kernel_size[0] // 2, kernel_size[1] // 2
        self.bias = bias

        self.conv = nn.Conv2d(
            in_channels=self.input_dim + self.hidden_dim,
            out_channels=4 * self.hidden_dim,
            kernel_size=self.kernel_size,
            padding=self.padding,
            bias=self.bias
        )

    def init_hidden(self, input_size, batch_size):
        return tuple(
            torch.zeros(*batch_size, self.hidden_dim, *input_size),
            torch.zeros(*batch_size, self.hidden_dim, *input_size),
        )

    def forward(self, input_tensor, cur_state):
        h_cur, c_cur = cur_state

        combined = torch.cat([input_tensor, h_cur], dim=-3)  # concatenate along channel axis
        combined_conv = self.conv(combined)

        cc_i, cc_f, cc_o, cc_g = torch.split(combined_conv, self.hidden_dim, dim=-3)
        i = torch.sigmoid(cc_i)
        f = torch.sigmoid(cc_f)
        o = torch.sigmoid(cc_o)
        g = torch.tanh(cc_g)

        c_next = f * c_cur + i * g
        h_next = o * torch.tanh(c_next)

        return h_next, c_next


class DRCCore(nn.Module):
    def __init__(self, num_layers, input_dim, hidden_dim, kernel_size=3, bias=True):
        super().__init__()
        self.num_layers = num_layers

        blocks = []
        for _ in range(self.num_layers):
            blocks.append(ConvLSTMCell(
                input_dim=input_dim,
                hidden_dim=hidden_dim,
                kernel_size=(kernel_size, kernel_size),
                bias=bias)
            )
        self.blocks = nn.ModuleList(blocks)

    def init_hidden(self, input_size, batch_size):
        hs, cs = [], []
        for block in self.blocks:
            h, c = block.init_hidden(input_size, batch_size)
            hs.append(h)
            cs.append(c)

        return torch.stack(hs), torch.stack(cs)

    def forward(self, x, hidden, num_repeats):
        if hidden is None:
            hidden = self.init_hidden(x.shape[-2:], x.shape[:-3])

        hs = [hidden[0][i] for i in range(self.num_layers)]
        cs = [hidden[1][i] for i in range(self.num_layers)]
        for _ in range(num_repeats):
            for i, block in enumerate(self.blocks):
                hs[i], cs[i] = block(x, (hs[i], cs[i]))

        return hs[-1], (torch.stack(hs), torch.stack(cs))


# simple model

class BaseModel(nn.Module):
    def __init__(self, env, args=None, action_length=None):
        super().__init__()
        self.action_length = env.action_length() if action_length is None else action_length

    def init_hidden(self, batch_size=None):
        return None

    def inference(self, x, hidden, **kwargs):
        # numpy array -> numpy array
        self.eval()
        with torch.no_grad():
            xt = tuple(to_torch(xx).unsqueeze(0) for xx in x)
            ht = tuple(to_torch(hh).unsqueeze(1) for hh in hidden) if hidden is not None else None
            outputs = self.forward(xt, ht, **kwargs)

        return tuple(
            [to_numpy(o).squeeze(0) for o in outputs[:-1]] + \
            [tuple(to_numpy(o).squeeze(1) for o in outputs[-1]) if outputs[-1] is not None else None]
        )


class RandomModel(BaseModel):
    def inference(self, x=None, hidden=None):
        return np.zeros(self.action_length), np.zeros(1), None


class LinearModel(BaseModel):
    def __init__(self, env, args=None, action_length=None):
        super().__init__(env, args, action_length)
        self.fc_p = nn.Linear(1, self.action_length, bias=True)
        self.fc_v = nn.Linear(1, 1, bias=True)

    def forward(self, x, hidden=None):
        return self.fc_p(x), self.fc_v(x), None


class DuelingNet(BaseModel):
    def __init__(self, env, args={}):
        super().__init__(env, args)

        self.input_size = env.observation()[0].shape

        layers, filters = args.get('layers', 3), args.get('filters', 32)
        internal_size = (filters, *self.input_size[1:])

        self.encoder = Encoder(self.input_size, filters)
        self.body = WideResNet(layers, filters)
        self.head_p = Head(internal_size, 2, self.action_length)
        self.head_v = Head(internal_size, 1, 1)

    def forward(self, x, hidden=None):
        h = self.encoder(x[0])
        h = self.body(h)
        h_p = self.head_p(h)
        h_v = self.head_v(h)

        return h_p, torch.tanh(h_v), None


class DRC(BaseModel):
    def __init__(self, env, args={}, action_length=None):
        super().__init__(env, args, action_length)
        self.input_size = env.observation()[0].shape

        layers, filters = args.get('layers', 3), args.get('filters', 32)
        internal_size = (filters, *self.input_size[1:])

        self.encoder = Encoder(self.input_size, filters)
        self.body = DRCCore(layers, filters, filters)
        self.head_p = Head(internal_size, 2, self.action_length)
        self.head_v = Head(internal_size, 1, 1)

    def init_hidden(self, batch_size=None):
        if batch_size is None:  # for inference
            with torch.no_grad():
                return to_numpy(self.body.init_hidden(self.input_size[1:], []))
        else:  # for training
            return self.body.init_hidden(self.input_size[1:], batch_size)

    def forward(self, x, hidden, num_repeats=1):
        h = self.encoder(x[0])
        h, hidden = self.body(h, hidden, num_repeats)
        h_p = self.head_p(h)
        h_v = self.head_v(h)

        return h_p, torch.tanh(h_v), hidden


class ModelCongress:
    def __init__(self, models):
        self.models = models

    def init_hidden(self, batch_size=None):
        return [m.init_hidden(batch_size) for m in self.models]

    def inference(self, x, hiddens):
        # conmputes mean value of outputs
        ps, vs, nhiddens = [], [], []
        for i, model in enumerate(self.models):
            with torch.no_grad():
                p, v, nhidden = model.inference(x, hiddens[i])
                ps.append(softmax(p))
                vs.append(v)
                nhiddens.append(nhidden)
        return np.log(np.mean(ps, axis=0) + 1e-8), np.mean(vs, axis=0), nhiddens