"""
 Copyright (c) 2018, salesforce.com, inc.
 All rights reserved.
 SPDX-License-Identifier: BSD-3-Clause
 For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
 
 Fact scoring networks.
 Code adapted from https://github.com/TimDettmers/ConvE/blob/master/model.py
"""

import copy

import torch
import torch.nn as nn
import torch.nn.functional as F
from functools import reduce
from operator import mul


class TripleE(nn.Module):
    def __init__(self, args, num_entities):
        super(TripleE, self).__init__()
        conve_args = copy.deepcopy(args)    
        conve_args.model = 'conve'
        self.conve_nn = ConvE(conve_args, num_entities)
        conve_state_dict = torch.load(args.conve_state_dict_path)
        conve_nn_state_dict = get_conve_nn_state_dict(conve_state_dict)
        self.conve_nn.load_state_dict(conve_nn_state_dict)

        complex_args = copy.deepcopy(args)
        complex_args.model = 'complex'
        self.complex_nn = ComplEx(complex_args)

        distmult_args = copy.deepcopy(args)
        distmult_args.model = 'distmult'
        self.distmult_nn = DistMult(distmult_args)

    def forward(self, e1, r, conve_kg, secondary_kgs):
        complex_kg = secondary_kgs[0]
        distmult_kg = secondary_kgs[1]
        return (self.conve_nn.forward(e1, r, conve_kg)
                + self.complex_nn.forward(e1, r, complex_kg)
                + self.distmult_nn.forward(e1, r, distmult_kg)) / 3

    def forward_fact(self, e1, r, conve_kg, secondary_kgs):
        complex_kg = secondary_kgs[0]
        distmult_kg = secondary_kgs[1]
        return (self.conve_nn.forward_fact(e1, r, conve_kg)
                + self.complex_nn.forward_fact(e1, r, complex_kg)
                + self.distmult_nn.forward_fact(e1, r, distmult_kg)) / 3

class HyperE(nn.Module):
    def __init__(self, args, num_entities):
        super(HyperE, self).__init__()
        self.conve_nn = ConvE(args, num_entities)
        conve_state_dict = torch.load(args.conve_state_dict_path)
        conve_nn_state_dict = get_conve_nn_state_dict(conve_state_dict)
        self.conve_nn.load_state_dict(conve_nn_state_dict)

        complex_args = copy.deepcopy(args)
        complex_args.model = 'complex'
        self.complex_nn = ComplEx(complex_args)

    def forward(self, e1, r, conve_kg, secondary_kgs):
        complex_kg = secondary_kgs[0]
        return (self.conve_nn.forward(e1, r, conve_kg)
                + self.complex_nn.forward(e1, r, complex_kg)) / 2

    def forward_fact(self, e1, r, e2, conve_kg, secondary_kgs):
        complex_kg = secondary_kgs[0]
        return (self.conve_nn.forward_fact(e1, r, e2, conve_kg)
                + self.complex_nn.forward_fact(e1, r, e2, complex_kg)) / 2

class ComplEx(nn.Module):
    def __init__(self, args):
        super(ComplEx, self).__init__()

    def forward(self, e1, r, kg):
        def dist_mult(E1, R, E2):
            return torch.mm(E1 * R, E2.transpose(1, 0))

        E1_real = kg.get_entity_embeddings(e1)
        R_real = kg.get_relation_embeddings(r)
        E2_real = kg.get_all_entity_embeddings()
        E1_img = kg.get_entity_img_embeddings(e1)
        R_img = kg.get_relation_img_embeddings(r)
        E2_img = kg.get_all_entity_img_embeddings()

        rrr = dist_mult(R_real, E1_real, E2_real)
        rii = dist_mult(R_real, E1_img, E2_img)
        iri = dist_mult(R_img, E1_real, E2_img)
        iir = dist_mult(R_img, E1_img, E2_real)
        S = rrr + rii + iri - iir
        S = F.sigmoid(S)
        return S

    def forward_fact(self, e1, r, e2, kg):
        def dist_mult_fact(E1, R, E2):
            return torch.sum(E1 * R * E2, dim=1, keepdim=True)

        E1_real = kg.get_entity_embeddings(e1)
        R_real = kg.get_relation_embeddings(r)
        E2_real = kg.get_entity_embeddings(e2)
        E1_img = kg.get_entity_img_embeddings(e1)
        R_img = kg.get_relation_img_embeddings(r)
        E2_img = kg.get_entity_img_embeddings(e2)

        rrr = dist_mult_fact(R_real, E1_real, E2_real)
        rii = dist_mult_fact(R_real, E1_img, E2_img)
        iri = dist_mult_fact(R_img, E1_real, E2_img)
        iir = dist_mult_fact(R_img, E1_img, E2_real)
        S = rrr + rii + iri - iir
        S = F.sigmoid(S)
        return S

class ConvE(nn.Module):
    def __init__(self, args, num_entities):
        super(ConvE, self).__init__()
        self.entity_dim = args.entity_dim
        self.relation_dim = args.relation_dim
        assert(args.emb_2D_d1 * args.emb_2D_d2 == args.entity_dim)
        assert(args.emb_2D_d1 * args.emb_2D_d2 == args.relation_dim)
        self.emb_2D_d1 = args.emb_2D_d1
        self.emb_2D_d2 = args.emb_2D_d2
        self.num_out_channels = args.num_out_channels
        self.w_d = args.kernel_size

        self.HiddenDropout = nn.Dropout(args.hidden_dropout_rate)
        self.FeatureDropout = nn.Dropout(args.feat_dropout_rate)

        # stride = 1, padding = 0, dilation = 1, groups = 1
        self.conv1 = nn.Conv2d(1, self.num_out_channels, (self.w_d, self.w_d), 1, 0)
        self.bn0 = nn.BatchNorm2d(1)
        self.bn1 = nn.BatchNorm2d(self.num_out_channels)
        self.bn2 = nn.BatchNorm1d(self.entity_dim)
        self.register_parameter('b', nn.Parameter(torch.zeros(num_entities)))
        h_out = 2 * self.emb_2D_d1 - self.w_d + 1
        w_out = self.emb_2D_d2 - self.w_d + 1
        self.feat_dim = self.num_out_channels * h_out * w_out
        self.fc = nn.Linear(self.feat_dim, self.entity_dim)

    def forward(self, e1, r, kg):
        E1 = kg.get_entity_embeddings(e1).view(-1, 1, self.emb_2D_d1, self.emb_2D_d2)
        R = kg.get_relation_embeddings(r).view(-1, 1, self.emb_2D_d1, self.emb_2D_d2)
        E2 = kg.get_all_entity_embeddings()

        stacked_inputs = torch.cat([E1, R], 2)
        stacked_inputs = self.bn0(stacked_inputs)

        X = self.conv1(stacked_inputs)
        # X = self.bn1(X)
        X = F.relu(X)
        X = self.FeatureDropout(X)
        X = X.view(-1, self.feat_dim)
        X = self.fc(X)
        X = self.HiddenDropout(X)
        X = self.bn2(X)
        X = F.relu(X)
        X = torch.mm(X, E2.transpose(1, 0))
        X += self.b.expand_as(X)

        S = F.sigmoid(X)
        return S

    def forward_fact(self, e1, r, e2, kg):
        """
        Compute network scores of the given facts.
        :param e1: [batch_size]
        :param r:  [batch_size]
        :param e2: [batch_size]
        :param kg:
        """
        # print(e1.size(), r.size(), e2.size())
        # print(e1.is_contiguous(), r.is_contiguous(), e2.is_contiguous())
        # print(e1.min(), r.min(), e2.min())
        # print(e1.max(), r.max(), e2.max())
        E1 = kg.get_entity_embeddings(e1).view(-1, 1, self.emb_2D_d1, self.emb_2D_d2)
        R = kg.get_relation_embeddings(r).view(-1, 1, self.emb_2D_d1, self.emb_2D_d2)
        E2 = kg.get_entity_embeddings(e2)

        stacked_inputs = torch.cat([E1, R], 2)
        stacked_inputs = self.bn0(stacked_inputs)

        X = self.conv1(stacked_inputs)
        # X = self.bn1(X)
        X = F.relu(X)
        X = self.FeatureDropout(X)
        X = X.view(-1, self.feat_dim)
        X = self.fc(X)
        X = self.HiddenDropout(X)
        X = self.bn2(X)
        X = F.relu(X)
        X = torch.matmul(X.unsqueeze(1), E2.unsqueeze(2)).squeeze(2)
        X += self.b[e2].unsqueeze(1)

        S = F.sigmoid(X)
        return S

class DistMult(nn.Module):
    def __init__(self, args):
        super(DistMult, self).__init__()

    def forward(self, e1, r, kg):
        E1 = kg.get_entity_embeddings(e1)
        R = kg.get_relation_embeddings(r)
        E2 = kg.get_all_entity_embeddings()
        S = torch.mm(E1 * R, E2.transpose(1, 0))
        S = F.sigmoid(S)
        return S

    def forward_fact(self, e1, r, e2, kg):
        E1 = kg.get_entity_embeddings(e1)
        R = kg.get_relation_embeddings(r)
        E2 = kg.get_entity_embeddings(e2)
        S = torch.sum(E1 * R * E2, dim=1, keepdim=True)
        S = F.sigmoid(S)
        return S

# Contextual Parameter Generator Class for ConvE and other methods
# network_structure: dimensions of input to all hidden layers of network
# - input is assumed to be first element of network_structure
# - last element is assumed to be last hidden layer of network
# output_shape: dimensions of the desired paired network parameters
# dropout: probability for dropout
# use_batch_norm: whether to use batch_norm
# batch_norm_momentum: momentum for batchnorm
# use_bias: whether CPG network should have bias
class ContextualParameterGenerator(nn.Module):
    def __init__(self, network_structure, output_shape, dropout, use_batch_norm=False,
                 batch_norm_momentum=0.99, use_bias=False):
        super(ContextualParameterGenerator, self).__init__()
        self.network_structure = network_structure
        self.output_shape = output_shape
        self.dropout = dropout
        self.use_batch_norm = use_batch_norm
        self.use_bias = use_bias
        print('use bias: {}'.format(self.use_bias))
        self.flattened_output = reduce(mul, output_shape, 1)

        self.projections = []
        layer_input = network_structure[0]
        for layer_output in self.network_structure[1:]:
            print('inside loop!')
            self.projections.append(nn.Linear(layer_input, layer_output, bias=self.use_bias))
            if use_batch_norm:
                self.projections.append(nn.BatchNorm1d(num_features=layer_output,
                                                       momentum=batch_norm_momentum))
            self.projections.append(nn.ReLU())
            self.projections.append(nn.Dropout(p=self.dropout))
            layer_input = layer_output

        self.projections.append(nn.Linear(layer_input, self.flattened_output, bias=self.use_bias))
        self.network = nn.Sequential(*self.projections)

    def forward(self, query_emb):
        flat_params = self.network(query_emb)
        params = flat_params.view([-1] + self.output_shape)
        # print('CPG shape: {}'.format(params.shape))
        return params

class CPG_ConvE(nn.Module):
    def __init__(self, args, num_entities):
        super(CPG_ConvE, self).__init__()
        self.entity_dim = args.entity_dim
        self.relation_dim = args.relation_dim
        assert(args.emb_2D_d1 * args.emb_2D_d2 == args.entity_dim)
        # assert(args.emb_2D_d1 * args.emb_2D_d2 == args.relation_dim)
        self.emb_2D_d1 = args.emb_2D_d1
        self.emb_2D_d2 = args.emb_2D_d2
        self.num_out_channels = args.num_out_channels
        self.w_d = args.kernel_size
        # Due to not being able to pass both int type and list type to argsparse
        # [-1] is equivalent to None. Thus, perform check for this
        if (len(args.cpg_conv_net) > 0) and (args.cpg_conv_net[0] == -1):
            self.cpg_conv_net = None
        else:
            self.cpg_conv_net = args.cpg_conv_net
        if (len(args.cpg_fc_net) > 0) and (args.cpg_fc_net[0] == -1):
            self.cpg_fc_net = None
        else:
            self.cpg_fc_net = args.cpg_fc_net
        self.cpg_dropout = args.cpg_dropout
        self.cpg_batch_norm = args.cpg_batch_norm
        self.cpg_batch_norm_momentum = args.cpg_batch_norm_momentum
        self.cpg_use_bias = args.cpg_use_bias


        self.HiddenDropout = nn.Dropout(args.hidden_dropout_rate)
        self.FeatureDropout = nn.Dropout(args.feat_dropout_rate)

        # stride = 1, padding = 0, dilation = 1, groups = 1
        if self.cpg_conv_net is None:
            self.conv1 = nn.Conv2d(1, self.num_out_channels, (self.w_d, self.w_d), 1, 0)
        self.bn0 = nn.BatchNorm2d(1)
        self.bn1 = nn.BatchNorm2d(self.num_out_channels)
        self.bn2 = nn.BatchNorm1d(self.entity_dim)
        self.register_parameter('b', nn.Parameter(torch.zeros(num_entities)))
        # ConvE baseline
        print('HI')
        if (self.cpg_conv_net is None) and (self.cpg_fc_net is None):
            h_out = 2 * self.emb_2D_d1 - self.w_d + 1
            print('CPG is None')
        # CPG-ConvE
        else:
            h_out = self.emb_2D_d1 - self.w_d + 1
        w_out = self.emb_2D_d2 - self.w_d + 1
        self.feat_dim = self.num_out_channels * h_out * w_out
        if self.cpg_fc_net is None:
            self.fc = nn.Linear(self.feat_dim, self.entity_dim)

        if self.cpg_conv_net is not None:
            self.conv_filter = ContextualParameterGenerator(network_structure=[self.relation_dim] + self.cpg_conv_net,
                                                            output_shape=[self.num_out_channels, 1, self.w_d, self.w_d],
                                                            dropout=self.cpg_dropout,
                                                            use_batch_norm=self.cpg_batch_norm,
                                                            batch_norm_momentum=self.cpg_batch_norm_momentum,
                                                            use_bias=self.cpg_use_bias)
            self.conv_bias = ContextualParameterGenerator(network_structure=[self.relation_dim] + self.cpg_conv_net,
                                                          output_shape=self.num_out_channels,
                                                          dropout=self.cpg_dropout,
                                                          use_batch_norm=self.cpg_batch_norm,
                                                          batch_norm_momentum=self.cpg_batch_norm_momentum,
                                                          use_bias=self.cpg_use_bias)
        if self.cpg_fc_net is not None:
            self.fc_weights = ContextualParameterGenerator(network_structure=[self.relation_dim] + self.cpg_fc_net,
                                                           output_shape=[self.feat_dim, self.entity_dim],
                                                           dropout=self.cpg_dropout,
                                                           use_batch_norm=self.cpg_batch_norm,
                                                           batch_norm_momentum=self.cpg_batch_norm_momentum,
                                                           use_bias=self.cpg_use_bias)
            self.fc_bias = ContextualParameterGenerator(network_structure=[self.relation_dim] + self.cpg_fc_net,
                                                        output_shape=[self.entity_dim],
                                                        dropout=self.cpg_dropout,
                                                        use_batch_norm=self.cpg_batch_norm,
                                                        batch_norm_momentum=self.cpg_batch_norm_momentum,
                                                        use_bias=self.cpg_use_bias)
            #self.fc_weights = nn.Linear(self.relation_dim, self.feat_dim * self.entity_dim, bias=False)
            #self.fc_bias = nn.Linear(self.relation_dim, self.entity_dim, bias=False)

    def forward(self, e1, r, kg):
        E1 = kg.get_entity_embeddings(e1).view(-1, 1, self.emb_2D_d1, self.emb_2D_d2)
        # possible that relation is no longer emb size 200
        emb_2D_d2 = int(self.relation_dim / self.emb_2D_d1)
        R = kg.get_relation_embeddings(r).view(-1, 1, self.emb_2D_d1, emb_2D_d2)
        E2 = kg.get_all_entity_embeddings()
        # print('#'*80)
        # print('cpg_fc_net: {} | cpg_conve_net: {}'.format(self.cpg_fc_net, self.cpg_conv_net))
        # print('#' * 80)
        if (self.cpg_fc_net is None) and (self.cpg_conv_net is None) and (self.entity_dim == self.relation_dim):
            stacked_inputs = torch.cat([E1, R], 2)
            # print('Stacking inputs!')
        else:
            # print('Inputs not stacked!')
            R = R.view(-1, self.relation_dim)
            stacked_inputs = E1
        stacked_inputs = self.bn0(stacked_inputs)
        # print('Batch+other stuff: {}'.format(stacked_inputs.size()))
        if self.cpg_conv_net is not None:
            X = nn.functional.conv2d(input=stacked_inputs,
                                     weight=self.conv_filter(R),
                                     bias=self.conv_bias(R))
        else:
            X = self.conv1(stacked_inputs)
        # X = self.bn1(X)
        X = F.relu(X)
        X = self.FeatureDropout(X)
        X = X.view(-1, self.feat_dim)

        if self.cpg_fc_net is not None:
            # print('X shape: {} | fc_weights shape: {} | fc_bias shape: {}'.format(X.size(),
            #                                                                       self.fc_weights(R).size(),
            #                                                                       self.fc_bias(R).size()))

            # X = nn.functional.linear(input=X,
            #                          weight=self.fc_weights(R))
            fc_weights = self.fc_weights(R)
            fc_weights = fc_weights.view(-1, self.feat_dim, self.entity_dim)
            # X = X.matmul(fc_weights)
            X = torch.einsum('ij, ijk-> ik', X, fc_weights)
            X += self.fc_bias(R)
        else:
            X = self.fc(X)
        X = self.HiddenDropout(X)
        X = self.bn2(X)
        X = F.relu(X)
        X = torch.mm(X, E2.transpose(1, 0))
        X += self.b.expand_as(X)

        S = F.sigmoid(X)
        # print('MEMORY ALLOCATED: {}'.format(torch.cuda.memory_allocated()))
        return S

    def forward_fact(self, e1, r, e2, kg):
        """
        Compute network scores of the given facts.
        :param e1: [batch_size]
        :param r:  [batch_size]
        :param e2: [batch_size]
        :param kg:
        """
        # print(e1.size(), r.size(), e2.size())
        # print(e1.is_contiguous(), r.is_contiguous(), e2.is_contiguous())
        # print(e1.min(), r.min(), e2.min())
        # print(e1.max(), r.max(), e2.max())
        E1 = kg.get_entity_embeddings(e1).view(-1, 1, self.emb_2D_d1, self.emb_2D_d2)
        # possible that relation is no longer emb size 200
        emb_2D_d2 = int(self.relation_dim / self.emb_2D_d1)
        R = kg.get_relation_embeddings(r).view(-1, 1, self.emb_2D_d1, emb_2D_d2)
        E2 = kg.get_entity_embeddings(e2)
        if (self.cpg_fc_net is None) and (self.cpg_conv_net is None) and (self.relation_dim == self.entity_dim):
            stacked_inputs = torch.cat([E1, R], 2)
        else:
            stacked_inputs = E1
            R = R.view(-1, self.relation_dim)
        stacked_inputs = self.bn0(stacked_inputs)
        if self.cpg_conv_net is not None:
            X = nn.functional.conv2d(input=stacked_inputs,
                                     weight=self.conv_filter(R),
                                     bias=self.conv_bias(R))
        else:
            X = self.conv1(stacked_inputs)
        # X = self.bn1(X)
        X = F.relu(X)
        X = self.FeatureDropout(X)
        X = X.view(-1, self.feat_dim)
        if self.cpg_fc_net is not None:
            fc_weights = self.fc_weights(R)
            X = torch.einsum('ij, ijk-> ik', X, fc_weights)
            X += self.fc_bias(R)
        else:
            X = self.fc(X)
        X = self.HiddenDropout(X)
        X = self.bn2(X)
        X = F.relu(X)
        X = torch.matmul(X.unsqueeze(1), E2.unsqueeze(2)).squeeze(2)
        X += self.b[e2].unsqueeze(1)

        S = F.sigmoid(X)
        return S

def get_conve_nn_state_dict(state_dict, is_cpg=False):
    conve_nn_state_dict = {}
    print('ConvE State dict: {}'.format(state_dict['state_dict'].keys()))
    param_names = ['mdl.b', 'mdl.conv1.weight', 'mdl.conv1.bias', 'mdl.bn0.weight', 'mdl.bn0.bias',
                   'mdl.bn0.running_mean', 'mdl.bn0.running_var', 'mdl.bn1.weight', 'mdl.bn1.bias',
                   'mdl.bn1.running_mean', 'mdl.bn1.running_var', 'mdl.bn2.weight', 'mdl.bn2.bias',
                   'mdl.bn2.running_mean', 'mdl.bn2.running_var']
    if is_cpg:
        param_names += ['mdl.fc_weights.network.0.weight', 'mdl.fc_bias.network.0.weight']
    else:
        param_names += ['mdl.fc.weight', 'mdl.fc.bias']

    for param_name in param_names:
        conve_nn_state_dict[param_name.split('.', 1)[1]] = state_dict['state_dict'][param_name]
    return conve_nn_state_dict

def get_conve_kg_state_dict(state_dict):
    kg_state_dict = dict()
    for param_name in ['kg.entity_embeddings.weight', 'kg.relation_embeddings.weight']:
        kg_state_dict[param_name.split('.', 1)[1]] = state_dict['state_dict'][param_name]
    return kg_state_dict

def get_complex_kg_state_dict(state_dict):
    kg_state_dict = dict()
    for param_name in ['kg.entity_embeddings.weight', 'kg.relation_embeddings.weight',
                       'kg.entity_img_embeddings.weight', 'kg.relation_img_embeddings.weight']:
        kg_state_dict[param_name.split('.', 1)[1]] = state_dict['state_dict'][param_name]
    return kg_state_dict

def get_distmult_kg_state_dict(state_dict):
    kg_state_dict = dict()
    for param_name in ['kg.entity_embeddings.weight', 'kg.relation_embeddings.weight']:
        kg_state_dict[param_name.split('.', 1)[1]] = state_dict['state_dict'][param_name]
    return kg_state_dict


