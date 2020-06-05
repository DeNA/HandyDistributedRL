# Copyright (c) 2020 DeNA Co., Ltd.
# Licensed under The MIT License [see LICENSE for details]
#
# Paper that proposed VTrace algorithm
# https://arxiv.org/abs/1802.01561
# Official code
# https://github.com/deepmind/scalable_agent/blob/6c0c8a701990fab9053fb338ede9c915c18fa2b1/vtrace.py

# training

import os
import time
import copy
import threading
import random
import signal
import bz2
import pickle
import yaml
from collections import deque

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as dist
import torch.optim as optim

import environment as gym
from model import to_torch, to_numpy, to_gpu_or_not, softmax, RandomModel
from model import DuelingNet as Model
from connection import MultiProcessWorkers, MultiThreadWorkers
from connection import accept_socket_connections
from worker import Workers


def make_batch(episodes, args):
    """Making training batch

    Args:
        episodes (Iterable): list of episodes
        args (dict): training configuration

    Returns:
        dict: PyTorch input and target tensors

    Note:
        Basic data shape is (T, B, P, ...) .
        (T is time length, B is batch size, P is player count)
    """

    datum = []  # obs, act, p, v, ret, adv, len
    steps = args['forward_steps']

    for ep in episodes:
        ep = pickle.loads(bz2.decompress(ep))

        # target player and turn index
        players = sorted([player for player in ep['value'].keys() if player >= 0])
        ep_train_length = len(ep['turn'])
        turn_candidates = 1 + max(0, ep_train_length - args['forward_steps'])  # change start turn by sequence length
        st = random.randrange(turn_candidates)
        ed = min(st + steps, len(ep['turn']))

        obs_sample = ep['observation'][ep['turn'][st]][st]
        if args['observation']:
            obs_zeros = tuple(np.zeros_like(o) for o in obs_sample)  # template for padding
            # transpose observation from (P, T, tuple) to (tuple, T, P)
            obs = []
            for i, _ in enumerate(obs_zeros):
                obs.append([])
                for t in range(st, ed):
                    obs[-1].append([])
                    for player in players:
                        obs[-1][-1].append(ep['observation'][player][t][i] if ep['observation'][player][t] is not None else obs_zeros[i])
        else:
            obs = tuple([[ep['observation'][ep['turn'][t]][t][i]] for t in range(st, ed)] for i, _ in enumerate(obs_sample))

        obs = tuple(np.array(o) for o in obs)

        # datum that is not changed by training configuration
        v = np.array(
            [[ep['value'][player][t] or 0 for player in players] for t in range(st, ed)],
            dtype=np.float32
        ).reshape(-1, len(players))
        tmsk = np.eye(len(players))[ep['turn'][st:ed]]
        pmsk = np.array(ep['pmask'][st:ed])
        vmsk = np.ones_like(tmsk) if args['observation'] else tmsk

        act = np.array(ep['action'][st:ed]).reshape(-1, 1)
        p = np.array(ep['policy'][st:ed])
        progress = np.arange(st, ed, dtype=np.float32) / len(ep['turn'])

        traj_steps = len(tmsk)
        ret = np.array(ep['reward'], dtype=np.float32).reshape(1, -1)

        # pad each array if step length is short
        if traj_steps < steps:
            pad_len = steps - traj_steps
            obs = tuple(np.pad(o, [(0, pad_len)] + [(0, 0)] * (len(o.shape) - 1), 'constant', constant_values=0) for o in obs)
            v = np.concatenate([v, np.tile(ret, [pad_len, 1])])
            tmsk = np.pad(tmsk, [(0, pad_len), (0, 0)], 'constant', constant_values=0)
            pmsk = np.pad(pmsk, [(0, pad_len), (0, 0)], 'constant', constant_values=1e32)
            vmsk = np.pad(vmsk, [(0, pad_len), (0, 0)], 'constant', constant_values=0)
            act = np.pad(act, [(0, pad_len), (0, 0)], 'constant', constant_values=0)
            p = np.pad(p, [(0, pad_len), (0, 0)], 'constant', constant_values=0)
            progress = np.pad(progress, [(0, pad_len)], 'constant', constant_values=1)

        datum.append((obs, tmsk, pmsk, vmsk, act, p, v, ret, progress))

    obs, tmsk, pmsk, vmsk, act, p, v, ret, progress = zip(*datum)

    obs = tuple(to_torch(o, transpose=True) for o in zip(*obs))
    tmsk = to_torch(tmsk, transpose=True)
    pmsk = to_torch(pmsk, transpose=True)
    vmsk = to_torch(vmsk, transpose=True)
    act = to_torch(act, transpose=True)
    p = to_torch(p, transpose=True)
    v = to_torch(v, transpose=True)
    ret = to_torch(ret, transpose=True)
    progress = to_torch(progress, transpose=True)

    return {
        'observation': obs, 'tmask': tmsk, 'pmask': pmsk, 'vmask': vmsk,
        'action': act, 'policy': p, 'value': v, 'return': ret, 'progress': progress,
    }


def forward_prediction(model, hidden, batch):
    """Forward calculation via neural network

    Args:
        model (torch.nn.Module): neural network
        hidden: initial hidden state
        batch (dict): training batch (output of make_batch() function)

    Returns:
        tuple: calculated policy and value
    """

    observations = batch['observation']
    time_length = observations[0].size(0)

    if hidden is None:
        # feed-forward neural network
        obs = tuple(o.view(-1, *o.size()[3:]) for o in observations)
        t_policies, t_values, _ = model(obs, None)
    else:
        # sequential computation with RNN
        bmasks = batch['tmask'] + batch['vmask']
        bmasks = tuple(bmasks.view(time_length, 1, bmasks.size(1), bmasks.size(2),
            *[1 for _ in range(len(h.size()) - 3)]) for h in hidden)

        t_policies, t_values = [], []
        for t in range(time_length):
            bmask = tuple(m[t] for m in bmasks)
            obs = tuple(o[t].view(-1, *o.size()[3:]) for o in observations)
            hidden = tuple(h * bmask[i] for i, h in enumerate(hidden))
            if observations[0].size(2) == 1:
                hid = tuple(h.sum(dim=2) for h in hidden)
            else:
                hid = tuple(h.view(h.size(0), -1, *h.size()[3:]) for h in hidden)
            t_policy, t_value, next_hidden = model(obs, hid)
            t_policies.append(t_policy)
            t_values.append(t_value)
            next_hidden = tuple(h.view(h.size(0), -1, observations[0].size(2), *h.size()[2:]) for h in next_hidden)
            hidden = tuple(hidden[i] * (1 - bmask[i]) + h * bmask[i] for i, h in enumerate(next_hidden))
        t_policies = torch.stack(t_policies)
        t_values = torch.stack(t_values)

    # gather turn player's policies
    t_policies = t_policies.view(*observations[0].size()[:3], t_policies.size(-1))
    t_policies = t_policies.mul(batch['tmask'].unsqueeze(-1)).sum(-2) - batch['pmask']

    # mask valid target values
    t_values = t_values.view(*observations[0].size()[:3])
    t_values = t_values.mul(batch['vmask'])

    return t_policies, t_values


def compose_losses(policies, values, log_selected_policies, advantages, value_targets, tmasks, vmasks, progress):
    """Caluculate loss value

    Returns:
        tuple: losses and statistic values and the number of training data
    """

    losses = {}
    dcnt = tmasks.sum().item()

    turn_advantages = advantages.mul(tmasks).sum(-1, keepdim=True)

    losses['p'] = (-log_selected_policies * turn_advantages).sum()
    losses['v'] = ((values - value_targets) ** 2).mul(vmasks).sum() / 2

    entropy = dist.Categorical(logits=policies).entropy().mul(tmasks.sum(-1))
    losses['ent'] = entropy.sum()

    losses['total'] = losses['p'] + losses['v'] + entropy.mul(1 - progress * 0.9).sum() * -3e-1

    return losses, dcnt


def vtrace_base(batch, model, hidden, args):
    t_policies, t_values = forward_prediction(model, hidden, batch)
    actions = batch['action']
    gmasks = batch['tmask'].sum(-1, keepdim=True)
    clip_rho_threshold, clip_c_threshold = 1.0, 1.0

    log_selected_b_policies = F.log_softmax(batch['policy'], dim=-1).gather(-1, actions) * gmasks
    log_selected_t_policies = F.log_softmax(t_policies     , dim=-1).gather(-1, actions) * gmasks

    # thresholds of importance sampling
    log_rhos = log_selected_t_policies.detach() - log_selected_b_policies
    rhos = torch.exp(log_rhos)
    clipped_rhos = torch.clamp(rhos, 0, clip_rho_threshold)
    cs = torch.clamp(rhos, 0, clip_c_threshold)
    values_nograd = t_values.detach()

    if values_nograd.size(2) == 2:  # two player zerosum game
        values_nograd_opponent = -torch.stack([values_nograd[:, :, 1], values_nograd[:, :, 0]], dim=-1)
        if args['observation']:
            values_nograd = (values_nograd + values_nograd_opponent) / 2
        else:
            values_nograd = values_nograd + values_nograd_opponent

    values_nograd = values_nograd * gmasks + batch['return'] * (1 - gmasks)

    return t_policies, t_values, log_selected_t_policies, values_nograd, clipped_rhos, cs


def vtrace(batch, model, hidden, args):
    # IMPALA
    # https://github.com/deepmind/scalable_agent/blob/master/vtrace.py

    t_policies, t_values, log_selected_t_policies, values_nograd, clipped_rhos, cs = vtrace_base(batch, model, hidden, args)
    returns = batch['return']
    time_length = batch['vmask'].size(0)

    if args['return'] == 'MC':
        # VTrace with naive advantage
        value_targets = returns
        advantages = clipped_rhos * (returns - values_nograd)
    elif args['return'] == 'TD0':
        values_t_plus_1 = torch.cat([values_nograd[1:], returns])
        deltas = clipped_rhos * (values_t_plus_1 - values_nograd)

        # compute Vtrace value target recursively
        vs_minus_v_xs = deque([deltas[-1]])
        for i in range(time_length - 2, -1, -1):
            vs_minus_v_xs.appendleft(deltas[i] + cs[i] * vs_minus_v_xs[0])
        vs_minus_v_xs = torch.stack(tuple(vs_minus_v_xs))
        vs = vs_minus_v_xs + values_nograd

        # compute policy advantage
        value_targets = vs
        vs_t_plus_1 = torch.cat([vs[1:], returns])
        advantages = clipped_rhos * (vs_t_plus_1 - values_nograd)
    elif args['return'] == 'TDLAMBDA':
        lmb = 0.7
        lambda_returns = deque([returns[-1]])
        for i in range(time_length - 1, 0, -1):
            lambda_returns.appendleft((1 - lmb) * values_nograd[i] + lmb * lambda_returns[0])
        lambda_returns = torch.stack(tuple(lambda_returns))

        value_targets = lambda_returns
        advantages = clipped_rhos * (value_targets - values_nograd)

    return compose_losses(
        t_policies, t_values, log_selected_t_policies, advantages, value_targets,
        batch['tmask'], batch['vmask'], batch['progress']
    )


class Batcher:
    def __init__(self, args, episodes, gpu):
        self.args = args
        self.episodes = episodes
        self.gpu = gpu
        self.shutdown_flag = False

        def selector():
            while True:
                yield self.select_episode()

        def worker(conn, bid):
            print('started batcher %d' % bid)
            episodes = []
            while not self.shutdown_flag:
                ep = conn.recv()
                episodes.append(ep)
                if len(episodes) >= self.args['batch_size']:
                    batch = make_batch(episodes, self.args)
                    conn.send((batch, len(episodes)))
                    episodes = []
            print('finished batcher %d' % bid)

        def postprocess(batch):
            return to_gpu_or_not(batch, self.gpu)

        # self.workers = MultiProcessWorkers(worker, selector(), self.args['num_batchers'], postprocess, buffer_length=self.args['batch_size'] * 3, num_receivers=2)
        self.workers = MultiThreadWorkers(worker, selector(), self.args['num_batchers'], postprocess)

    def run(self):
        self.workers.start()

    def select_episode(self):
        while True:
            ep_idx = random.randrange(min(len(self.episodes), self.args['maximum_episodes']))
            accept_rate = 1 - (len(self.episodes) - 1 - ep_idx) / self.args['maximum_episodes']
            if random.random() < accept_rate:
                return self.episodes[ep_idx]

    def batch(self):
        return self.workers.recv()

    def shutdown(self):
        self.shutdown_flag = True
        self.workers.shutdown()


class Trainer:
    def __init__(self, args, model):
        self.episodes = deque()
        self.args = args
        self.gpu = torch.cuda.device_count()
        self.model = model
        self.defalut_lr = 3e-8
        self.data_cnt_ema = self.args['batch_size'] * self.args['forward_steps']
        self.params = list(self.model.parameters())
        lr = self.defalut_lr * self.data_cnt_ema
        self.optimizer = optim.Adam(self.params, lr=lr, weight_decay=1e-5) if len(self.params) > 0 else None
        self.steps = 0
        self.lock = threading.Lock()
        self.batcher = Batcher(self.args, self.episodes, self.gpu)
        self.updated_model = None, 0
        self.update_flag = False
        self.shutdown_flag = False

    def update(self):
        if len(self.episodes) < self.args['minimum_episodes']:
            return None, 0  # return None before training
        self.update_flag = True
        while True:
            time.sleep(0.1)
            model, steps = self.recheck_update()
            if model is not None:
                break
        return model, steps

    def report_update(self, model, steps):
        self.lock.acquire()
        self.update_flag = False
        self.updated_model = model, steps
        self.lock.release()

    def recheck_update(self):
        self.lock.acquire()
        flag = self.update_flag
        self.lock.release()
        return (None, -1) if flag else self.updated_model

    def shutdown(self):
        self.shutdown_flag = True
        self.batcher.shutdown()

    def train(self):
        if self.optimizer is None:  # non-parametric model
            print()
            return

        batch_cnt, data_cnt, loss_sum = 0, 0, {}
        train_model = self.model
        if self.gpu:
            if self.gpu > 1:
                train_model = nn.DataParallel(self.model)
            train_model.cuda()
        train_model.train()

        while data_cnt == 0 or not (self.update_flag or self.shutdown_flag):
            # episodes were only tuple of arrays
            batch = self.batcher.batch()
            batch_size = batch['value'].size(1)
            player_count = batch['value'].size(2)
            hidden = to_gpu_or_not(self.model.init_hidden([batch_size, player_count]), self.gpu)

            losses, dcnt = vtrace(batch, train_model, hidden, self.args)

            self.optimizer.zero_grad()
            losses['total'].backward()
            nn.utils.clip_grad_norm_(self.params, 4.0)
            self.optimizer.step()

            batch_cnt += 1
            data_cnt += dcnt
            for k, l in losses.items():
                loss_sum[k] = loss_sum.get(k, 0.0) + l.item()

            self.steps += 1

        print('loss = %s' % ' '.join([k + ':' + '%.3f' % (l / data_cnt) for k, l in loss_sum.items()]))

        self.data_cnt_ema = self.data_cnt_ema * 0.8 + data_cnt / (1e-2 + batch_cnt) * 0.2
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = self.defalut_lr * self.data_cnt_ema * (1 + self.steps * 1e-5)
        self.model.cpu()
        self.model.eval()
        return copy.deepcopy(self.model)

    def run(self):
        print('waiting training')
        while not self.shutdown_flag:
            if len(self.episodes) < self.args['minimum_episodes']:
                time.sleep(1)
                continue
            if self.steps == 0:
                self.batcher.run()
                print('started training')
            model = self.train()
            self.report_update(model, self.steps)
        print('finished training')


class Learner:
    def __init__(self, args):
        self.args = args
        random.seed(args['seed'])
        self.env = gym.make()
        self.shutdown_flag = False

        # trained datum
        self.model_era = 0
        self.model_class = self.env.net() if hasattr(self.env, 'net') else Model
        self.model = RandomModel(self.env)
        train_model = self.model_class(self.env, args)

        # generated datum
        self.num_episodes = 0

        # evaluated datum
        self.results = {}
        self.num_results = 0

        # multiprocess or remote connection
        self.workers = Workers(args)

        # thread connection
        self.trainer = Trainer(args, train_model)

    def shutdown(self):
        self.shutdown_flag = True
        self.trainer.shutdown()
        self.workers.shutdown()
        for thread in self.threads:
            thread.join()

    def model_path(self, model_id):
        return os.path.join('models', str(model_id) + '.pth')

    def update_model(self, model, steps):
        # get latest model and save it
        print('updated model(%d)' % steps)
        self.model_era += 1
        self.model = model
        os.makedirs('models', exist_ok=True)
        torch.save(model.state_dict(), self.model_path(self.model_era))

    def feed_episodes(self, episodes):
        # store generated episodes
        self.trainer.episodes.extend([e for e in episodes if e is not None])
        while len(self.trainer.episodes) > self.args['maximum_episodes']:
            self.trainer.episodes.popleft()

    def feed_results(self, results):
        # store evaluation results
        for model_id, reward in results:
            if reward is None:
                continue
            if model_id not in self.results:
                self.results[model_id] = {}
            if reward not in self.results[model_id]:
                self.results[model_id][reward] = 0
            self.results[model_id][reward] += 1

    def update(self):
        # call update to every component
        if self.model_era not in self.results:
            print('win rate = Nan (0)')
        else:
            distribution = self.results[self.model_era]
            results = {k: distribution[k] for k in sorted(distribution.keys(), reverse=True)}
            # output evaluation results
            n, win = 0, 0.0
            for r, cnt in results.items():
                n += cnt
                win += (r + 1) / 2 * cnt
            print('win rate = %.3f (%.1f / %d)' % (win / n, win, n))
        model, steps = self.trainer.update()
        if model is None:
            model = self.model
        self.update_model(model, steps)

    def server(self):
        # central conductor server
        # returns as list if getting multiple requests as list
        print('started server')
        prev_update_episodes = self.args['minimum_episodes']
        while True:
            # no update call before storings minimum number of episodes + 1 age
            next_update_episodes = prev_update_episodes + self.args['update_episodes']
            while not self.shutdown_flag and self.num_episodes < next_update_episodes:
                conn, (req, data) = self.workers.recv()
                multi_req = isinstance(data, list)
                if not multi_req:
                    data = [data]
                send_data = []
                if req == 'gargs':
                    # genatation configuration
                    for _ in data:
                        args = {
                            'episode_id': self.num_episodes,
                            'player': self.num_episodes % 2,
                            'model_id': {}
                        }
                        num_congress = int(1 + np.log2(self.model_era + 1)) if self.args['congress'] else 1
                        for p in range(2):
                            if p == args['player']:
                                args['model_id'][p] = [self.model_era]
                            else:
                                args['model_id'][p] = [self.model_era]  # [random.randrange(self.model_era + 1) for _ in range(num_congress)]
                        send_data.append(args)

                        self.num_episodes += 1
                        if self.num_episodes % 100 == 0:
                            print(self.num_episodes, end=' ', flush=True)
                elif req == 'eargs':
                    # evaluation configuration
                    for _ in data:
                        args = {
                            'model_id': self.model_era,
                            'player': self.num_results % 2,
                        }
                        send_data.append(args)
                        self.num_results += 1
                elif req == 'episode':
                    # report generated episodes
                    self.feed_episodes(data)
                    send_data = [True] * len(data)
                elif req == 'result':
                    # report evaluation results
                    self.feed_results(data)
                    send_data = [True] * len(data)
                elif req == 'model':
                    for model_id in data:
                        if model_id == self.model_era:
                            model = self.model
                        else:
                            try:
                                model = self.model_class(self.env, self.args)
                                model.load_state_dict(torch.load(self.model_path(model_id)))
                            except:
                                # return latest model if failed to load specified model
                                pass
                        send_data.append(model)
                if not multi_req and len(send_data) == 1:
                    send_data = send_data[0]
                self.workers.send(conn, send_data)
            prev_update_episodes = next_update_episodes
            self.update()
        print('finished server')

    def entry_server(self):
        port = 9999
        print('started entry server %d' % port)
        conn_acceptor = accept_socket_connections(port=port, timeout=0.3)
        total_gids, total_eids, worker_cnt = [], [], 0
        while not self.shutdown_flag:
            conn = next(conn_acceptor)
            if conn is not None:
                entry_args = conn.recv()
                print('accepted entry from %s!' % entry_args['host'])
                gids, eids = [], []
                # divide workers into generator/worker
                for _ in range(entry_args['num_process']):
                    if len(total_gids) * self.args['eworker_rate'] < len(total_eids) - 1:
                        gids.append(worker_cnt)
                        total_gids.append(worker_cnt)
                    else:
                        eids.append(worker_cnt)
                        total_eids.append(worker_cnt)
                    worker_cnt += 1
                args = copy.deepcopy(self.args)
                args['worker'] = entry_args
                args['gids'], args['eids'] = gids, eids
                conn.send(args)
                conn.close()
        print('finished entry server')

    def run(self):
        try:
            # open threads
            self.threads = [threading.Thread(target=self.trainer.run)]
            if self.args['remote']:
                self.threads.append(threading.Thread(target=self.entry_server))
            for thread in self.threads:
                thread.daemon = True
                thread.start()
            # open generator, evaluator
            self.workers.run()
            self.server()

        finally:
            self.shutdown()


if __name__ == '__main__':
    with open('config.yaml') as f:
        args = yaml.load(f)
    print(args)

    train_args = args['train_args']
    env_args = args['env_args']
    train_args['env'] = env_args

    gym.prepare(env_args)
    learner = Learner(train_args)
    learner.run()