#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2021 The OpenRL Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""""""
import torch
import torch.nn as nn

from openrl.buffers.utils.util import get_critic_obs_space
from openrl.modules.networks.base_value_policy_network import BaseValuePolicyNetwork
from openrl.modules.networks.utils.cnn import CNNBase
from openrl.modules.networks.utils.mix import MIXBase
from openrl.modules.networks.utils.mlp import MLPBase
from openrl.modules.networks.utils.rnn import RNNLayer
from openrl.modules.networks.utils.util import init
from openrl.modules.networks.utils.vdn import VDNBase
from openrl.utils.util import check_v2 as check


class VDNNetwork(BaseValuePolicyNetwork):
    def __init__(
        self,
        cfg,
        input_space,
        action_space,
        device=torch.device("cpu"),
        use_half=False,
        extra_args=None,
    ) -> None:
        super(VDNNetwork, self).__init__(cfg, device)
        self.hidden_size = cfg.hidden_size

        self._gain = cfg.gain
        self._use_orthogonal = cfg.use_orthogonal
        self._activation_id = cfg.activation_id
        self._use_policy_active_masks = cfg.use_policy_active_masks
        self._use_naive_recurrent_policy = cfg.use_naive_recurrent_policy
        self._use_recurrent_policy = cfg.use_recurrent_policy
        self._recurrent_N = cfg.recurrent_N
        self.use_half = use_half
        self.tpdv = dict(dtype=torch.float32, device=device)
        self.n_agent = cfg.num_agents
        self.action_n = action_space.n
        init_method = [nn.init.xavier_uniform_, nn.init.orthogonal_][
            self._use_orthogonal
        ]

        obs_shape = get_critic_obs_space(input_space)

        if "Dict" in obs_shape.__class__.__name__:
            self._mixed_obs = True
            self.base = MIXBase(cfg, obs_shape, cnn_layers_params=cfg.cnn_layers_params)
        else:
            self._mixed_obs = False
            self.base = (
                CNNBase(cfg, obs_shape)
                if len(obs_shape) == 3
                else MLPBase(
                    cfg,
                    obs_shape,
                    use_attn_internal=cfg.use_attn_internal,
                    use_cat_self=True,
                )
            )

        input_size = self.base.output_size

        if self._use_naive_recurrent_policy or self._use_recurrent_policy:
            self.rnn = RNNLayer(
                input_size,
                self.hidden_size,
                self._recurrent_N,
                self._use_orthogonal,
                rnn_type=cfg.rnn_type,
            )
            input_size = self.hidden_size

        def init_(m):
            return init(m, init_method, lambda x: nn.init.constant_(x, 0))

        self.q_out = init_(nn.Linear(input_size, action_space.n))
        self.q_tot = VDNBase()
        if use_half:
            self.half()
        self.to(device)

    def forward(self, forward_type, *args, **kwargs):
        if forward_type == "original":
            return self.get_actions(*args, **kwargs)
        elif forward_type == "eval_actions":
            return self.eval_actions(*args, **kwargs)
        elif forward_type == "eval_actions_target":
            return self.eval_actions_target(*args, **kwargs)
        elif forward_type == "get_values":
            return self.get_values(*args, **kwargs)
        elif forward_type == "eval_values":
            return self.eval_values(*args, **kwargs)
        else:
            raise NotImplementedError

    def get_actions(self, *args, **kwargs):
        raise NotImplementedError

    def eval_actions(
        self, obs, rnn_states, action, masks, action_masks, active_masks=None
    ):
        if self._mixed_obs:
            for key in obs.keys():
                obs[key] = check(obs[key]).to(**self.tpdv)
        else:
            obs = check(obs).to(**self.tpdv)
        rnn_states = check(rnn_states).to(**self.tpdv)
        masks = check(masks).to(**self.tpdv)

        features = self.base(obs)

        if self._use_naive_recurrent_policy or self._use_recurrent_policy:
            features, rnn_states = self.rnn(features, rnn_states, masks)

        q_values = self.q_out(features)
        q_values_ = q_values.reshape(-1, self.n_agent, self.action_n)
        action = action.reshape(-1, self.n_agent, 1)
        action_ = torch.from_numpy(action).long()

        q_values_ = torch.gather(q_values_, dim=2, index=action_)

        q_tot_value = self.q_tot(q_values_)

        q_tot_value = q_tot_value.view(-1, 1)

        return q_tot_value

    def get_values(self, obs, rnn_states, masks, action_masks=None):
        if self._mixed_obs:
            for key in obs.keys():
                obs[key] = check(obs[key]).to(**self.tpdv)
        else:
            obs = check(obs).to(**self.tpdv)
        rnn_states = check(rnn_states).to(**self.tpdv)
        masks = check(masks).to(**self.tpdv)

        features = self.base(obs)

        if self._use_naive_recurrent_policy or self._use_recurrent_policy:
            features, rnn_states = self.rnn(features, rnn_states, masks)

        q_values = self.q_out(features)

        if action_masks is not None:
            q_values[action_masks == 0] = -1e10

        return q_values, rnn_states

    def eval_actions_target(
        self, obs, rnn_states, action, masks, action_masks, active_masks=None
    ):
        if self._mixed_obs:
            for key in obs.keys():
                obs[key] = check(obs[key]).to(**self.tpdv)
        else:
            obs = check(obs).to(**self.tpdv)
        rnn_states = check(rnn_states).to(**self.tpdv)
        masks = check(masks).to(**self.tpdv)

        features = self.base(obs)

        if self._use_naive_recurrent_policy or self._use_recurrent_policy:
            features, rnn_states = self.rnn(features, rnn_states, masks)

        q_values = self.q_out(features)
        q_values_ = q_values.reshape(-1, self.n_agent, self.action_n)

        q_values_ = q_values_.max(-1)[0]

        q_tot_value = self.q_tot(q_values_).view(-1, 1)

        return q_tot_value

    def eval_values(self, obs, rnn_states, masks, action_masks=None):
        if self._mixed_obs:
            for key in obs.keys():
                obs[key] = check(obs[key]).to(**self.tpdv)
        else:
            if isinstance(obs, dict):
              obs = obs["policy"]
            obs = check(obs).to(**self.tpdv)
        rnn_states = check(rnn_states).to(**self.tpdv)
        masks = check(masks).to(**self.tpdv)

        features = self.base(obs)

        if self._use_naive_recurrent_policy or self._use_recurrent_policy:
            features, rnn_states = self.rnn(features, rnn_states, masks)

        q_values = self.q_out(features)

        if action_masks is not None:
            q_values[action_masks == 0] = -1e10

        return q_values, rnn_states
