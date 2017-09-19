import os
import sys
import numpy as np
import random
import gym
import math
import time

import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
import torch.multiprocessing as mp

from model import Model

class ReplayMemory(object):
    def __init__(self, capacity):
        self.capacity = capacity
        self.memory = []

    def push(self, events):
        for event in zip(*events):
            self.memory.append(event)
            if len(self.memory)>self.capacity:
                del self.memory[0]

    def clear(self):
        self.memory = []

    def sample(self, batch_size):
        samples = zip(*random.sample(self.memory, batch_size))
        return map(lambda x: torch.cat(x, 0), samples)

def ensure_shared_grads(model, shared_model):
    for param, shared_param in zip(model.parameters(), shared_model.parameters()):
        if shared_param.grad is not None:
            pass
        shared_param._grad = param.grad

def normal(x, mu, std):
    a = (-1*(x-mu).pow(2)/(2*std)).exp()
    b = 1/(2*std*np.pi).sqrt()
    return a*b

def train(rank, params, traffic_light, counter, shared_model, shared_grad_buffers, shared_obs_stats, test_n):
    torch.manual_seed(params.seed)
    env = gym.make(params.env_name)
    num_inputs = env.observation_space.shape[0]
    num_outputs = env.action_space.shape[0]
    model = Model(num_inputs, num_outputs)

    memory = ReplayMemory(params.exploration_size)

    state = env.reset()
    state = Variable(torch.Tensor(state).unsqueeze(0))
    done = True

    episode_length = 0
    while True:
        episode_length += 1
        model.load_state_dict(shared_model.state_dict())

        w = -1
        av_reward = 0
        nb_runs = 0
        reward_0 = 0
        t = -1
        while w < params.exploration_size:
            t+=1
            states = []
            actions = []
            rewards = []
            values = []
            returns = []
            advantages = []
            av_reward = 0
            cum_reward = 0
            cum_done = 0

            # Perform K steps
            for step in range(params.num_steps):
                w+=1
                shared_obs_stats.observes(state)
                state = shared_obs_stats.normalize(state)
                states.append(state)
                mu, sigma_sq, v = model(state)
                eps = torch.randn(mu.size())
                action = (mu + sigma_sq.sqrt()*Variable(eps))
                actions.append(action)
                values.append(v)
                env_action = action.data.squeeze().numpy()
                state, reward, done, _ = env.step(env_action)
                done = (done or episode_length >= params.max_episode_length)
                cum_reward += reward
                reward = max(min(reward, 1), -1)
                rewards.append(reward)
                if done:
                    cum_done += 1
                    av_reward += cum_reward
                    cum_reward = 0
                    episode_length = 0
                    state = env.reset()
                state = Variable(torch.Tensor(state).unsqueeze(0))
                if done:
                    break

            # one last step
            R = torch.zeros(1, 1)
            if not done:
                _,_,v = model(state)
                R = v.data
            # compute returns and GAE(lambda) advantages:
            values.append(Variable(R))
            R = Variable(R)
            A = Variable(torch.zeros(1, 1))
            for i in reversed(range(len(rewards))):
                td = rewards[i] + params.gamma*values[i+1].data[0,0] - values[i].data[0,0]
                A = float(td) + params.gamma*params.gae_param*A
                advantages.insert(0, A)
                R = A + values[i]
                returns.insert(0, R)
            # store usefull info:
            memory.push([states, actions, returns, advantages])

        # policy grad updates:
        av_reward /= float(cum_done+1)
        model_old = Model(num_inputs, num_outputs)
        model_old.load_state_dict(model.state_dict())
        if t==0:
            reward_0 = av_reward-(1e-2)
        #batch_states, batch_actions, batch_returns, batch_advantages = memory.sample(params.batch_size)
        for k in range(params.num_epoch):
            # load new model
            model.load_state_dict(shared_model.state_dict())
            model.zero_grad()
            # get initial signal
            signal_init = traffic_light.get()
            # new mini_batch
            batch_states, batch_actions, batch_returns, batch_advantages = memory.sample(params.batch_size)
            # old probas
            mu_old, sigma_sq_old, v_pred_old = model_old(batch_states.detach())
            probs_old = normal(batch_actions, mu_old, sigma_sq_old)
            # new probas
            mu, sigma_sq, v_pred = model(batch_states)
            probs = normal(batch_actions, mu, sigma_sq)
            # ratio
            ratio = probs/(1e-10+probs_old)
            # clip loss
            surr1 = ratio * torch.cat([batch_advantages]*num_outputs,1) # surrogate from conservative policy iteration
            surr2 = ratio.clamp(1-params.clip, 1+params.clip) * torch.cat([batch_advantages]*num_outputs,1)
            loss_clip = -torch.mean(torch.min(surr1, surr2))
            # value loss
            vfloss1 = (v_pred - batch_returns)**2
            v_pred_clipped = v_pred_old + (v_pred - v_pred_old).clamp(-params.clip, params.clip)
            vfloss2 = (v_pred_clipped - batch_returns)**2
            loss_value = 0.5*torch.mean(torch.max(vfloss1, vfloss2))
            # entropy
            loss_ent = -params.ent_coeff*torch.mean(probs*torch.log(probs+1e-5))
            # total
            total_loss = (loss_clip + loss_value + loss_ent)
            #print(total_loss.data[0])
            # before step, update old_model:
            model_old.load_state_dict(model.state_dict())
            # prepare for step
            total_loss.backward(retain_variables=True)
            #ensure_shared_grads(model, shared_model)
            #shared_model.cum_grads()
            shared_grad_buffers.add_gradient(model)

            counter.increment()

            # wait for a new signal to continue
            while traffic_light.get() == signal_init:
                pass

        test_n += 1
        memory.clear()
