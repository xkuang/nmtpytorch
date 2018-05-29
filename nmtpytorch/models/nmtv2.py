# -*- coding: utf-8 -*-
import logging

import torch
import torch.nn as nn

from ..layers import TextEncoder, GRUDecoder
from ..utils.misc import get_n_params
from ..vocabulary import Vocabulary
from ..utils.topology import Topology
from ..utils.ml_metrics import MeanReciprocalRank, Loss
from ..datasets import MultimodalDataset
from ..metrics import Metric

logger = logging.getLogger('nmtpytorch')

# NOTE
# This is not working right now, please do not use it.


class NMTv2(nn.Module):
    """An improved NMT that uses CuDNN RNN primitives in the stacked decoder."""
    supports_beam_search = True

    def set_defaults(self):
        self.defaults = {
            'emb_dim': 128,             # Source and target embedding sizes
            'emb_maxnorm': None,        # Normalize embeddings l2 norm to 1
            'emb_gradscale': False,     # Scale embedding gradients w.r.t. batch frequency
            'enc_dim': 256,             # Encoder hidden size
            'enc_type': 'gru',          # Encoder type (gru|lstm)
            'n_encoders': 1,            # Number of stacked encoders
            'dec_dim': 256,             # Decoder hidden size
            'dec_type': 'gru',          # Decoder type (gru|lstm)
            'dec_init': 'mean_ctx',     # How to initialize decoder (zero/mean_ctx/feats)
            'dec_init_activ': 'tanh',   # The non-linearity for *_ctx and feats inits
            'dec_init_size': None,      # feature vector dimensionality for
                                        # dec_init == 'feats'
            'att_type': 'mlp',          # Attention type (mlp|dot)
            'att_temp': 1.,             # Attention temperature
            'att_activ': 'tanh',        # Attention non-linearity (all torch nonlins)
            'att_mlp_bias': False,      # Enables bias in attention mechanism
            'att_bottleneck': 'ctx',    # Bottleneck dimensionality (ctx|hid)
            'att_transform_ctx': True,  # Transform annotations before attention
            'concat_outputs': True,     # Whether the output of the pre-attn RNN and
                                        # the context should be concatenated or not
                                        # Defines the input size of the post-attn RNN
            'n_layers_preatt': 1,       # Pre-attention RNN decoder count
            'n_layers_postatt': 1,      # Post-attention RNN decoder count
            'dropout_emb': 0,           # Simple dropout to source embeddings
            'dropout_ctx': 0,           # Simple dropout to source encodings
            'dropout_out': 0,           # Simple dropout to decoder output
            'dropout_enc': 0,           # Intra-encoder dropout if n_encoders > 1
            'dropout_dec': 0,           # Intra-decoder dropout if n_layers_* > 1
            'tied_emb': False,          # Share embeddings: (False|2way|3way)
            'direction': None,          # Network directionality, i.e. en->de
            'max_len': 80,              # Reject sentences where 'bucket_by' length > 80
            'bucket_by': None,          # A key like 'en' to define w.r.t which dataset
                                        # the batches will be sorted
        }

    def __init__(self, opts):
        super().__init__()

        # opts -> config file sections {.model, .data, .vocabulary, .train}
        self.opts = opts

        # Vocabulary objects
        self.vocabs = {}

        # Each auxiliary loss should be stored inside this dictionary
        # in order to be taken into account by the mainloop for multi-tasking
        self.aux_loss = {}

        # Setup options
        self.opts.model = self.set_model_options(opts.model)

        # Parse topology & languages
        self.topology = Topology(self.opts.model['direction'])

        # Load vocabularies here
        for name, fname in self.opts.vocabulary.items():
            self.vocabs[name] = Vocabulary(fname, name=name)

        # Inherently non multi-lingual aware
        slangs = self.topology.get_src_langs()
        tlangs = self.topology.get_trg_langs()
        if slangs:
            self.sl = slangs[0]
            self.src_vocab = self.vocabs[self.sl]
            self.n_src_vocab = len(self.src_vocab)
        if tlangs:
            self.tl = tlangs[0]
            self.trg_vocab = self.vocabs[self.tl]
            self.n_trg_vocab = len(self.trg_vocab)

        # Textual context size is always equal to enc_dim * 2 since
        # it is the concatenation of forward and backward hidden states
        if 'enc_dim' in self.opts.model:
            self.ctx_sizes = {str(self.sl): self.opts.model['enc_dim'] * 2}

        # Check vocabulary sizes for 3way tying
        if self.opts.model['tied_emb'] == '3way':
            assert self.n_src_vocab == self.n_trg_vocab, \
                "The vocabulary sizes do not match for 3way tied embeddings."

        # Need to be set for early-stop evaluation
        # NOTE: This should come from config or elsewhere
        self.val_refs = self.opts.data['val_set'][self.tl]

    def __repr__(self):
        s = super().__repr__() + '\n'
        for vocab in self.vocabs.values():
            s += "{}\n".format(vocab)
        s += "{}\n".format(get_n_params(self))
        return s

    def set_model_options(self, model_opts):
        self.set_defaults()
        for opt, value in model_opts.items():
            if opt in self.defaults:
                # Override defaults from config
                self.defaults[opt] = value
            else:
                logger.info('Warning: unused model option: {}'.format(opt))
        return self.defaults

    def reset_parameters(self):
        for name, param in self.named_parameters():
            if param.requires_grad and 'bias' not in name:
                nn.init.kaiming_normal(param.data)

    def setup(self, is_train=True):
        """Sets up NN topology by creating the layers."""
        ########################
        # Create Textual Encoder
        ########################
        self.enc = TextEncoder(
            input_size=self.opts.model['emb_dim'],
            hidden_size=self.opts.model['enc_dim'],
            n_vocab=self.n_src_vocab,
            rnn_type=self.opts.model['enc_type'],
            src_sorted_batches=True,
            dropout_emb=self.opts.model['dropout_emb'],
            dropout_ctx=self.opts.model['dropout_ctx'],
            dropout_rnn=self.opts.model['dropout_enc'],
            num_layers=self.opts.model['n_encoders'],
            emb_maxnorm=self.opts.model['emb_maxnorm'],
            emb_gradscale=self.opts.model['emb_gradscale'])

        ################
        # Create Decoder
        ################
        self.dec = GRUDecoder(
            input_size=self.opts.model['emb_dim'],
            hidden_size=self.opts.model['dec_dim'],
            ctx_size_dict=self.ctx_sizes,
            ctx_name=str(self.sl),
            n_vocab=self.n_trg_vocab,
            n_layers_preatt=self.opts.model['n_layers_preatt'],
            n_layers_postatt=self.opts.model['n_layers_postatt'],
            tied_emb=self.opts.model['tied_emb'],
            dec_init=self.opts.model['dec_init'],
            dec_init_size=self.opts.model['dec_init_size'],
            dec_init_activ=self.opts.model['dec_init_activ'],
            att_type=self.opts.model['att_type'],
            att_activ=self.opts.model['att_activ'],
            att_bottleneck=self.opts.model['att_bottleneck'],
            att_temp=self.opts.model['att_temp'],
            att_mlp_bias=self.opts.model['att_mlp_bias'],
            dropout_out=self.opts.model['dropout_out'],
            dropout_dec=self.opts.model['dropout_dec'],
            emb_maxnorm=self.opts.model['emb_maxnorm'],
            emb_gradscale=self.opts.model['emb_gradscale'])

        # Share encoder and decoder weights
        if self.opts.model['tied_emb'] == '3way':
            self.enc.emb.weight = self.dec.emb.weight

    def load_data(self, split, batch_size, mode='train'):
        """Loads the requested dataset split."""
        dataset = MultimodalDataset(
            data=self.opts.data['{}_set'.format(split)],
            mode=mode, batch_size=batch_size,
            vocabs=self.vocabs, topology=self.topology,
            bucket_by=self.opts.model['bucket_by'],
            max_len=self.opts.model['max_len'])
        logger.info(dataset)
        return dataset

    def get_bos(self, batch_size):
        """Returns a representation for <bos> embeddings for decoding."""
        return torch.LongTensor(batch_size).fill_(self.trg_vocab['<bos>'])

    def encode(self, batch):
        """Encodes all inputs and returns a dictionary.

        Arguments:
            batch (dict): A batch of samples with keys designating the
                information sources.

        Returns:
            dict:
                A dictionary where keys are source modalities compatible
                with the data loader and the values are tuples where the
                elements are encodings and masks. The mask can be ``None``
                if the relevant modality does not require a mask.
        """
        return {str(self.sl): self.enc(batch[self.sl])}

    def forward(self, batch):
        """Computes the forward-pass of the network and returns batch loss.

        Arguments:
            batch (dict): A batch of samples with keys designating the source
                and target modalities.

        Returns:
            Variable:
                A scalar loss normalized w.r.t batch size and token counts.
        """
        # Get loss dict
        result = self.dec(self.encode(batch), batch[self.tl])
        result['n_items'] = torch.nonzero(batch[self.tl][1:]).shape[0]
        return result

    def test_performance(self, data_loader, dump_file=None):
        """Computes test set loss over the given DataLoader instance."""
        loss = Loss()
        mrr = MeanReciprocalRank(self.n_trg_vocab)

        for batch in data_loader:
            batch.to_gpu(volatile=True)
            out = self.forward(batch)
            loss.update(out['loss'], out['n_items'])
            mrr.update(batch[self.tl][1:].data, out['logps'])

        return [
            Metric('LOSS', loss.get(), higher_better=False),
            Metric('MRR', mrr.normalized_mrr(), higher_better=True),
        ]
