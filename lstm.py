"""
The class objects for the UnconditionalLSTM and ConditionalLSTM.

In general, these objects take input parameters such as the
embedding dimension, hidden dimension, and number of layers. On
.fit(), the models iteratively save checkpoints to a log
directory whose root is specified by `log_base_dir.` A log_level
parameter determines the behavior of saving the checkpoints.

LOG LEVEL 0: no logs of any kind
LOG LEVEL 1: write logs to ./logs/debug
LOG LEVEL 2: write logs to new directory w/ username & time
"""


import os
import math
import json
import torch
import pickle
import shutil
import getpass
import datetime
import numpy as np
from torch import nn
from tqdm import tqdm
from data_utils import decode, split_encoding_by_measure
import torch.nn.functional as F
from collections import defaultdict
from tensorboardX import SummaryWriter
from torch.utils.data import DataLoader

class UnconditionalLSTM(nn.Module):
    def __init__(self, embed_dim, hidden_dim, num_layers=2, dropout=0.5,
                 vocab_size=128, log_level=0, log_suffix=None, log_base_dir="./logs"):
        #Initialize the module constructor
        super(UnconditionalLSTM, self).__init__()

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        self.vocab_size = vocab_size

        # Encodes the (pitch, dur, adv) tuples
        self.token_embedding = nn.Embedding(self.vocab_size, embed_dim)

        # Encodes the position within each tuple, i.e. [0, 1, 2, 0, 1, 2, ...]
        self.pos_embedding = nn.Embedding(3, embed_dim)

        # NOTE: input dimension is 2 * embed_dim because we have embeddings for both
        # the token IDs and the positional IDs
        self.lstm = nn.LSTM(2 * embed_dim, hidden_dim, num_layers=num_layers, dropout=dropout)

        self.proj = nn.Linear(hidden_dim, self.vocab_size)

        self.optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)

        logdir = None
        if log_level==1:
            logdir = os.path.join(log_base_dir, 'debug')
            # Clear out the debug directory
            if os.path.exists(logdir):
                shutil.rmtree(logdir)

            os.mkdir(logdir)

        elif log_level==2:
            user = getpass.getuser().lower()
            date = str(datetime.datetime.now().date())
            time = str(datetime.datetime.now().time()).split('.')[0].replace(':', '-')

            logdir_name = '{}_{}_{}'.format(user, date, time)
            logdir = os.path.join(log_base_dir, logdir_name)
            if log_suffix is not None:
                logdir += log_suffix
            os.mkdir(logdir)

            args_string = "Embed dimension: {}" + \
                          "\nHidden dimension: {}" + \
                          "\nNum layers: {}" + \
                          "\nDropout: {}"
            args_string = args_string.format(embed_dim, hidden_dim, num_layers, dropout)

            with open(os.path.join(logdir, 'args.txt'), 'w') as file:
                file.write(args_string)

        self.prepare_logdir(logdir)

    def prepare_logdir(self, logdir=None):
        if logdir is not None:
            self.logdir = logdir
            self.train_sample_dir = os.path.join(self.logdir, 'train_samples')
            self.checkpoints_dir = os.path.join(self.logdir, 'checkpoints')

            os.mkdir(self.train_sample_dir)
            os.mkdir(self.checkpoints_dir)

            self.log_writer = SummaryWriter(self.logdir, flush_secs=100)

    def forward(self, token_ids):
        '''
        Args:
            token_ids: size is (batch_size, sequence_length)
        '''
        batch_size, seq_len = token_ids.shape

        token_embeds = self.token_embedding(token_ids)

        # Permute into (seq_len, batch, embed_size)
        token_embeds = token_embeds.permute(1, 0, 2)

        # The position ids are just 0, 1, and 2 repeated for as long
        # as the sequence length
        pos_ids = torch.tensor([0, 1, 2]).repeat(batch_size, math.ceil(seq_len/3))[:, :seq_len]
        pos_ids = pos_ids.to(self.device)
        pos_embeds = self.pos_embedding(pos_ids)
        pos_embeds = pos_embeds.permute(1, 0, 2)

        full_embeds = torch.cat((token_embeds, pos_embeds), dim=2)

        lstm_out, _ = self.lstm(full_embeds)

        projected = self.proj(lstm_out)

        return projected

    def fit(self, dataset, batch_size=8, num_epochs=10, save_interval=10000, validation_dataset=None):
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=4)

        loss_fn = nn.CrossEntropyLoss()
        global_step = 0
        for idx in range(num_epochs):
            with tqdm(dataloader, desc='Running batches', total=math.ceil(len(dataset)/batch_size)) as progbar:
                for batch in progbar:

                    token_ids, _, _ = batch

                    token_ids = token_ids.to(self.device)

                    inputs, labels = token_ids[:, :-1], token_ids[:, 1:]

                    out = self.forward(inputs)

                    # The class dimension needs to go in the middle for the CrossEntropyLoss
                    out = out.permute(0, 2, 1)

                    # And the labels need to be (batch, additional_dims)
                    labels = labels.permute(1, 0)

                    loss = loss_fn(out, labels)
                    progbar.set_postfix(Loss=loss.item())
                    progbar.set_description("[Epoch {}/{}]. Running batches...".format(idx, num_epochs))

                    loss.backward()
                    self.optimizer.step()
                    self.optimizer.zero_grad()

                    self.log_writer.add_scalar("loss", loss, global_step)
                    global_step += 1

                    if global_step%save_interval == 0:
                        self.save_checkpoint(global_step, generate_sample=True)

            # save after each epoch
            self.save_checkpoint(global_step, generate_sample=True)

            if validation_dataset is not None:
                self.validate(validation_dataset, batch_size, global_step)

    def validate(self, validation_dataset, batch_size, global_step):
        """
        Runs the model against a validation dataset and writes validation
        loss to tensorboard log file.
        """

        self.eval()
        validation_dataloader = DataLoader(validation_dataset, batch_size=batch_size, shuffle=False,
                                           num_workers=0)

        loss_fn = nn.CrossEntropyLoss()
        cumulative_loss = torch.zeros(1).to(self.device)
        val_step = 0
        with tqdm(validation_dataloader, desc='Validating...', total=math.ceil(len(validation_dataset) / batch_size)) as progbar:
            for batch in progbar:

                with torch.no_grad():

                    token_ids, _, _ = batch

                    token_ids = token_ids.to(self.device)

                    inputs, labels = token_ids[:, :-1], token_ids[:, 1:]

                    out = self.forward(inputs)

                    # The class dimension needs to go in the middle for the CrossEntropyLoss
                    out = out.permute(0, 2, 1)

                    # And the labels need to be (batch, additional_dims)
                    labels = labels.permute(1, 0)

                    cumulative_loss += loss_fn(out, labels)
                    val_step += 1
                    progbar.set_postfix(Val_Loss=(cumulative_loss.item()/val_step))

        self.log_writer.add_scalar("val_loss", cumulative_loss / val_step, global_step)

        self.train()

    def generate_measure_encodings(self, dataset, logdir, batch_size=8):
        """
        Generates the measure encoding lookup object used for training the conditional
        model. The output file is a pickled multi-dictionary object that is on the order
        of ~3 GB. A buffer dictionary is used to repeatedly dump contents to a file in
        order to avoid memory issues.
        """
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=0)

        track_id_to_measure_encodings = defaultdict(lambda: defaultdict(list))
        buffer_dict = defaultdict(lambda: defaultdict(list))

        # Threshold of entries before dumping to a file
        buffer_threshold = 100
        buffer_threshold_increment = 100

        self.eval()
        with torch.no_grad():
            for idx, batch in enumerate(tqdm(dataloader, desc='Generating measure encodings', total=math.ceil(len(dataset)/batch_size))):
                token_ids, measure_ids, track_ids = batch

                token_ids = token_ids.to(self.device)
                batch_size, seq_len = token_ids.shape

                token_embeds = self.token_embedding(token_ids)

                # Permute into (seq_len, batch, embed_size)
                token_embeds = token_embeds.permute(1, 0, 2)

                # The position ids are just 0, 1, and 2 repeated for as long
                # as the sequence length
                pos_ids = torch.tensor([0, 1, 2]).repeat(batch_size, math.ceil(seq_len/3))[:, :seq_len]
                pos_ids = pos_ids.to(self.device)
                pos_embeds = self.pos_embedding(pos_ids)
                pos_embeds = pos_embeds.permute(1, 0, 2)

                full_embeds = torch.cat((token_embeds, pos_embeds), dim=2)

                lstm_out, _ = self.lstm(full_embeds)

                # We need the lstm output to be (batch_size, seq_len, hidden_dim)
                lstm_out = lstm_out.permute(1, 0, 2).cpu().numpy().astype(np.float16).tolist()

                track_ids = track_ids.cpu().numpy()
                measure_ids = measure_ids.cpu().numpy()

                # First, we add all of the model hidden states, index by track and measure ID
                for batch_idx in range(batch_size):
                    for seq_len_idx in range(seq_len):
                        track_id = track_ids[batch_idx][seq_len_idx]
                        measure_id = measure_ids[batch_idx][seq_len_idx]

                        # once threshold is reached, dump buffer_dict contents of PRIOR tracks/measures
                        # then raise the threshold, empty the buffer, and continue onward
                        # this is necessary to keep memory footprint low
                        if track_id >= buffer_threshold:
                            print("Buffer threshold reached! {} tracks".format(buffer_threshold))
                            buffer_threshold += buffer_threshold_increment

                            print("Dumping buffer dict...")
                            for buffer_t_id in buffer_dict:  # buffer track
                                for buffer_m_id in buffer_dict[buffer_t_id]:  # buffer measure
                                    measure_hidden_states = buffer_dict[buffer_t_id][buffer_m_id]

                                    # Take the average to get compact representation
                                    track_id_to_measure_encodings[buffer_t_id][buffer_m_id] = torch.mean(torch.tensor(measure_hidden_states), dim=0)

                                # Convert the track to a normal dict
                                track_id_to_measure_encodings[buffer_t_id] = dict(track_id_to_measure_encodings[buffer_t_id])

                            # De-allocate buffer and start a new one
                            del buffer_dict
                            buffer_dict = defaultdict(lambda: defaultdict(list))

                        model_hidden = lstm_out[batch_idx][seq_len_idx]
                        buffer_dict[track_id][measure_id].append(model_hidden)

            # Final dump of buffer dict
            for track_id in buffer_dict:
                for measure_id in buffer_dict[track_id]:
                    measure_hidden_states = buffer_dict[track_id][measure_id]

                    track_id_to_measure_encodings[track_id][measure_id] = torch.mean(torch.tensor(measure_hidden_states), dim=0)

                # Convert the track to a normal dict
                track_id_to_measure_encodings[track_id] = dict(track_id_to_measure_encodings[track_id])

            # Convert the whole thing to a normal dict
            track_id_to_measure_encodings = dict(track_id_to_measure_encodings)

            # Save measure encodings to logdir
            measure_encodings_path = os.path.join(logdir, 'measure_encodings.pkl')

            print("Saving measure encodings to {}...".format(measure_encodings_path))
            with open(measure_encodings_path, 'wb') as file:
                pickle.dump(track_id_to_measure_encodings, file)


    def save_checkpoint(self, global_step, generate_sample=False):
        '''
        Saves the model state dict, and will generate a sample if specified
        '''
        checkpoint_name = os.path.join(self.checkpoints_dir, "model_checkpoint_step_{}.pt".format(global_step))
        torch.save(self.state_dict(), checkpoint_name)

        if generate_sample:
            generation = self.generate(length=120)
            stream = decode(generation)
            stream.write('midi', os.path.join(self.train_sample_dir, 'train_sample_checkpoint_step_{}.mid'.format(global_step)))

    def generate(self, condition=[60, 8, 8], k=None, temperature=1, length=100):
        '''
        If 'k' is None: sample over all tokens in vocabulary
        If temperature == 0: perform greedy generation
        '''
        # remove regularization for generation
        self.eval()

        prev = torch.tensor(condition).unsqueeze(0)
        prev = prev.to(self.device)
        output = prev

        with torch.no_grad():
            for i in tqdm(range(length), leave=False):

                logits = self.forward(output)
                logits = logits.to(self.device)

                if temperature == 0:
                    prev = torch.argmax(logits[-1][0]).reshape(1, 1)

                else:
                    logits[-1][0] /= temperature

                    # Take the last logits, and mask all but the top k
                    masked = self.mask_logits(logits[-1], k=k)

                    log_probs = F.softmax(masked, dim=1)

                    prev = torch.multinomial(log_probs, num_samples=1)

                output = torch.cat((output, prev), dim=1)

        output = output.cpu().numpy().tolist()[0]

        self.train()

        return output

    def evaluate(self, test_dataset, batch_size=8):
        """
        Evaluate the model against (typically) a test dataset. Returns
        average (mean) cross entropy loss against that dataset.
        """

        self.eval()
        dataloader = DataLoader(test_dataset, batch_size=batch_size, shuffle=False, num_workers=4)

        cumulative_loss = torch.zeros(1).to(self.device)
        global_step = 0
        loss_fn = nn.CrossEntropyLoss()
        with tqdm(dataloader, desc='Running batches', total=math.ceil(len(test_dataset)/batch_size)) as progbar:
            for batch in progbar:

                with torch.no_grad():
                    token_ids, _, _ = batch

                    token_ids = token_ids.to(self.device)

                    inputs, labels = token_ids[:, :-1], token_ids[:, 1:]

                    out = self.forward(inputs)

                    # The class dimension needs to go in the middle for the CrossEntropyLoss
                    out = out.permute(0, 2, 1)

                    # And the labels need to be (batch, additional_dims)
                    labels = labels.permute(1, 0)

                    loss = loss_fn(out, labels)
                    cumulative_loss += loss
                    global_step += 1

                    progbar.set_postfix(Loss=loss.item())

        mean_loss = cumulative_loss / global_step

        self.train()

        return mean_loss

    def mask_logits(self, logits, k=None):
        if k is None:
            return logits
        else:
            values = torch.topk(logits, k)[0]
            batch_mins = values[:, -1].view(-1, 1).expand_as(logits)
            return torch.where(logits < batch_mins,
                               torch.ones_like(logits) * -1e10,
                               logits)


class ConditionalLSTM(nn.Module):
    """
    Similar to Unconditioned except conditions generation on measure encodings accessed
    with a measure encoding lookup object (measure_enc_lookup) set in .fit().
    """
    def __init__(self, embed_dim, hidden_dim, measure_enc_dim, num_layers=2,
                 dropout=0.5, vocab_size=128, log_level=0, log_suffix=None, log_base_dir='./logs'):
        #Initialize the module constructor
        super(ConditionalLSTM, self).__init__()

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        self.vocab_size = vocab_size

        # The dimension of the measure encodings
        self.measure_enc_dim = measure_enc_dim
        self.measure_enc_lookup = None

        # Encodes the (pitch, dur, adv) tuples
        self.token_embedding = nn.Embedding(self.vocab_size, embed_dim)

        # Encodes the position within each tuple, i.e. [0, 1, 2, 0, 1, 2, ...]
        self.pos_embedding = nn.Embedding(3, embed_dim)

        # Projects the measure encodings into an embedding space
        self.measure_enc_proj = nn.Linear(measure_enc_dim, embed_dim)

        # NOTE: input dimension is 3 * embed_dim because we have embeddings for both
        # the token IDs, the positional IDs, and the bass-track measure encodings
        self.lstm = nn.LSTM(3 * embed_dim, hidden_dim, num_layers=num_layers, dropout=dropout)

        self.proj = nn.Linear(hidden_dim, self.vocab_size)

        self.optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)

        logdir = None
        if log_level==1:
            logdir = os.path.join(log_base_dir, 'debug')
            # Clear out the debug directory
            if os.path.exists(logdir):
                shutil.rmtree(logdir)

            os.mkdir(logdir)
            print("Logging to {}".format(logdir))

        elif log_level==2:
            user = getpass.getuser().lower()
            date = str(datetime.datetime.now().date())
            time = str(datetime.datetime.now().time()).split('.')[0].replace(':', '-')

            logdir_name = '{}_conditional_{}_{}'.format(user, date, time)
            logdir = os.path.join(log_base_dir, logdir_name)
            if log_suffix is not None:
                logdir += log_suffix
            os.mkdir(logdir)
            print("Logging to {}".format(logdir))

            args_string = "Embed dimension: {}" + \
                          "\nHidden dimension: {}" + \
                          "\nNum layers: {}" + \
                          "\nDropout: {}"
            args_string = args_string.format(embed_dim, hidden_dim, num_layers, dropout)

            with open(os.path.join(logdir, 'args.txt'), 'w') as file:
                file.write(args_string)

        self.prepare_logdir(logdir)

    def prepare_logdir(self, logdir=None):
        if logdir is not None:
            self.logdir = logdir
            self.train_sample_dir = os.path.join(self.logdir, 'train_samples')
            self.checkpoints_dir = os.path.join(self.logdir, 'checkpoints')

            os.mkdir(self.train_sample_dir)
            os.mkdir(self.checkpoints_dir)

            self.log_writer = SummaryWriter(self.logdir, flush_secs=100)

    def forward(self, token_ids, measure_ids, track_ids):
        '''
        Args:
            token_ids: size is (batch_size, sequence_length)
        '''
        batch_size, seq_len = token_ids.shape

        token_embeds = self.token_embedding(token_ids)

        # Permute into (seq_len, batch, embed_size)
        token_embeds = token_embeds.permute(1, 0, 2)

        # The position ids are just 0, 1, and 2 repeated for as long
        # as the sequence length
        pos_ids = torch.tensor([0, 1, 2]).repeat(batch_size, math.ceil(seq_len/3))[:, :seq_len]
        pos_ids = pos_ids.to(self.device)
        pos_embeds = self.pos_embedding(pos_ids)
        pos_embeds = pos_embeds.permute(1, 0, 2)

        if self.measure_enc_lookup is not None:
            measure_encs = torch.zeros(batch_size, seq_len, self.measure_enc_dim)
            # print("Beginning measure encoding lookup...")
            for batch_idx in range(batch_size):
                for seq_len_idx in range(seq_len):
                    measure_id = measure_ids[batch_idx][seq_len_idx]
                    track_id = track_ids[batch_idx][seq_len_idx]

                    measures = self.measure_enc_lookup.get(track_id)
                    if measures is None:
                        # print("Track ID {} has no bass".format(track_id))
                        continue
                    else:
                        enc = measures.get(measure_id)
                        if enc is None:
                            # print("Measure ID {} in track {} has no bass".format(measure_id, track_id))
                            continue
                        else:
                            measure_encs[batch_idx][seq_len_idx] = self.measure_enc_lookup[track_id][measure_id]
            # print("\tDone!")

        else:
            measure_encs = torch.zeros(batch_size, seq_len, self.measure_enc_dim)

        measure_encs = measure_encs.to(self.device)
        measure_embeds = self.measure_enc_proj(measure_encs)
        measure_embeds = measure_embeds.permute(1, 0, 2)

        full_embeds = torch.cat((token_embeds, pos_embeds, measure_embeds), dim=2)

        lstm_out, _ = self.lstm(full_embeds)

        projected = self.proj(lstm_out)

        return projected

    def fit(self, dataset, batch_size=8, num_epochs=10, save_interval=10000, measure_enc_dir=None,
            validation_dataset=None):
        """
        Train the conditioned LSTM using measure encodings from an unconditioned bass track
        model (these are created prior and are stored in a measure encoding object located
        at `measure_enc_dir.` Use generate_measure_encodings.py to create this object.
        """
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=0)

        if measure_enc_dir is not None:
            measure_encodings_path = os.path.join(measure_enc_dir, 'measure_encodings.pkl')
            print("Getting measure encoding lookup from {}...".format(measure_encodings_path))
            with open(measure_encodings_path, 'rb') as file:
                self.measure_enc_lookup = pickle.load(file)
                print("\tSuccess!")

        loss_fn = nn.CrossEntropyLoss()
        global_step = 0
        for idx in range(num_epochs):
            with tqdm(dataloader, desc='Running batches', total=math.ceil(len(dataset)/batch_size)) as progbar:
                for batch in progbar:

                    token_ids, measure_ids, track_ids = batch

                    token_ids = token_ids.to(self.device)

                    inputs, labels = token_ids[:, :-1], token_ids[:, 1:]
                    measure_ids, track_ids = measure_ids[:, :-1].numpy(), track_ids[:, :-1].numpy()

                    out = self.forward(inputs, measure_ids, track_ids)

                    # The class dimension needs to go in the middle for the CrossEntropyLoss
                    out = out.permute(0, 2, 1)

                    # And the labels need to be (batch, additional_dims)
                    labels = labels.permute(1, 0)

                    loss = loss_fn(out, labels)
                    progbar.set_postfix(Loss=loss.item())
                    progbar.set_description("[Epoch {}/{}]. Running batches...".format(idx, num_epochs))

                    loss.backward()
                    self.optimizer.step()
                    self.optimizer.zero_grad()

                    self.log_writer.add_scalar("loss", loss, global_step)
                    global_step += 1

                    if global_step%save_interval == 0:
                        self.save_checkpoint(global_step, generate_sample=False, measure_ids=measure_ids,
                                             track_ids=track_ids)

            # save after each epoch
            self.save_checkpoint(global_step, generate_sample=False, measure_ids=measure_ids, track_ids=track_ids)

            if validation_dataset is not None:
                self.validate(validation_dataset, batch_size, global_step)

    def validate(self, validation_dataset, batch_size, global_step):
        """
        Run an evaluation epoch on the validation_dataset.
        """

        self.eval()
        validation_dataloader = DataLoader(validation_dataset, batch_size=batch_size, shuffle=False,
                                           num_workers=0)

        loss_fn = nn.CrossEntropyLoss()
        cumulative_loss = torch.zeros(1).to(self.device)
        val_step = 0
        with tqdm(validation_dataloader, desc='Validating...', total=math.ceil(len(validation_dataset) / batch_size)) as progbar:
            for batch in progbar:

                with torch.no_grad():

                    token_ids, measure_ids, track_ids = batch

                    token_ids = token_ids.to(self.device)

                    inputs, labels = token_ids[:, :-1], token_ids[:, 1:]
                    measure_ids, track_ids = measure_ids[:, :-1].numpy(), track_ids[:, :-1].numpy()

                    out = self.forward(inputs, measure_ids, track_ids)

                    # The class dimension needs to go in the middle for the CrossEntropyLoss
                    out = out.permute(0, 2, 1)

                    # And the labels need to be (batch, additional_dims)
                    labels = labels.permute(1, 0)

                    cumulative_loss += loss_fn(out, labels)
                    val_step += 1
                    progbar.set_postfix(Val_Loss=(cumulative_loss.item()/val_step))

        self.log_writer.add_scalar("val_loss", cumulative_loss / val_step, global_step)

        self.train()

    def save_checkpoint(self, global_step, generate_sample=False, measure_ids=None, track_ids=None):
        '''
        Saves the model state dict, and will generate a sample if specified
        '''
        checkpoint_name = os.path.join(self.checkpoints_dir, "model_checkpoint_step_{}.pt".format(global_step))
        torch.save(self.state_dict(), checkpoint_name)

        # if generate_sample:
        #     generation = self.generate(length=120, measure_ids=measure_ids, track_ids=track_ids)
        #     stream = decode(generation)
        #     stream.write('midi', os.path.join(self.train_sample_dir, 'train_sample_checkpoint_step_{}.mid'.format(global_step)))

    def generate(self, melody_condition=[60, 8, 8], bassline_condition=[36, 8, 8], bassline_model=None, k=None,
                 bass_temp=1, bass_length=120, melody_temp=1, melody_length=240):
        '''
        If 'k' is None: sample over all tokens in vocabulary
        If temperature == 0: perform greedy generation
        '''
        with torch.no_grad():
            # If we have a bassline model, then we generate its output first
            if bassline_model is not None:
                bassline_model_output = bassline_model.generate(condition=bassline_condition, k=k, 
                                                                temperature=bass_temp, length=bass_length)

                # We need to cut off the last 3 tokens to account for the condition at the start
                bassline_model_output = bassline_model_output[:-3]

                bass_output_idxs_by_measure = split_encoding_by_measure(bassline_model_output)

                num_bass_measures = len(bass_output_idxs_by_measure)

                bass_token_ids = torch.tensor(bassline_model_output).reshape(1, len(bassline_model_output))
                bass_token_ids = bass_token_ids.to(bassline_model.device)
                batch_size, seq_len = bass_token_ids.shape

                bass_token_embeds = self.token_embedding(bass_token_ids)

                # Permute into (seq_len, batch, embed_size)
                bass_token_embeds = bass_token_embeds.permute(1, 0, 2)

                # The position ids are just 0, 1, and 2 repeated for as long
                # as the sequence length
                bass_pos_ids = torch.tensor([0, 1, 2]).repeat(batch_size, math.ceil(seq_len/3))[:, :seq_len]
                bass_pos_ids = bass_pos_ids.to(self.device)
                bass_pos_embeds = self.pos_embedding(bass_pos_ids)
                bass_pos_embeds = bass_pos_embeds.permute(1, 0, 2)

                bass_full_embeds = torch.cat((bass_token_embeds, bass_pos_embeds), dim=2)

                bass_lstm_out = bassline_model.lstm(bass_full_embeds)[0].squeeze(1)

                bass_measure_encodings = []
                for output_idxs in bass_output_idxs_by_measure:
                    bass_encodings = bass_lstm_out[output_idxs]

                    bass_measure_encodings.append(torch.mean(bass_encodings, dim=0))
                bass_measure_encodings = torch.stack(bass_measure_encodings, dim=0)

            # BEGIN CONDITIONAL GENERATION
            # remove regularization for generation
            self.eval()

            prev = torch.tensor(melody_condition).unsqueeze(0)
            prev = prev.to(self.device)
            output = prev

            measure_encs = torch.stack(3*[bass_measure_encodings[0]], dim=0).unsqueeze(0)
            measure_encs = measure_encs.to(self.device)

            cur_measure = 0

            # The conditioning is a quarter note, so that's the offset within the measure we 
            # begin at!
            measure_offset = 1

            # Index an advance token into this to get the number of quarter notes it represents
            all_timings = np.arange(0, 16, 0.125)

            for i in tqdm(range(melody_length), leave=False):
                # NOTE: if i%3 == 2, then we are generating an advance token and need to pay attention!!

                batch_size, seq_len = output.shape
                token_embeds = self.token_embedding(output)

                # Permute into (seq_len, batch, embed_size)
                token_embeds = token_embeds.permute(1, 0, 2)

                # The position ids are just 0, 1, and 2 repeated for as long
                # as the sequence length
                pos_ids = torch.tensor([0, 1, 2]).repeat(batch_size, math.ceil(seq_len/3))[:, :seq_len]
                pos_ids = pos_ids.to(self.device)
                pos_embeds = self.pos_embedding(pos_ids)
                pos_embeds = pos_embeds.permute(1, 0, 2)

                # The measure encoding comes from 'bass_measure_encodings' created above
                measure_embeds = self.measure_enc_proj(measure_encs)
                measure_embeds = measure_embeds.permute(1, 0, 2)

                full_embeds = torch.cat((token_embeds, pos_embeds, measure_embeds), dim=2)

                lstm_out, _ = self.lstm(full_embeds)

                logits = self.proj(lstm_out)
                logits = logits.to(self.device)

                if melody_temp == 0:
                    prev = torch.argmax(logits[-1][0]).reshape(1, 1)

                else:
                    logits[-1][0] /= melody_temp

                    # Take the last logits, and mask all but the top k
                    masked = self.mask_logits(logits[-1], k=k)

                    log_probs = F.softmax(masked, dim=1)

                    prev = torch.multinomial(log_probs, num_samples=1)

                output = torch.cat((output, prev), dim=1)

                # We add the encoding of the current measure to measure_encs tensor --
                # but if we generate more measures of melody than bass we just keep using
                # the final measure_encodings
                cur_measure_enc = bass_measure_encodings[min(cur_measure, num_bass_measures-1)].unsqueeze(0).unsqueeze(0)
                measure_encs = torch.cat((measure_encs, cur_measure_enc), dim=1)

                # If the current generation idx%3 == 2, then we're generating an advance token!
                if i%3 == 2:
                    advance_idx = prev.item()
                    measure_offset += all_timings[advance_idx]
                    # We always generate in 4/4
                    if measure_offset > 4:
                        cur_measure += 1
                        measure_offset = measure_offset%4

                        if cur_measure > num_bass_measures:
                            break

                # print("Final output shape: ", output.shape)
                # print("Final measure encs shape: ", measure_encs.shape)
                # assert False

        melody_model_output = output.cpu().numpy().tolist()[0]

        self.train()

        return bassline_model_output, melody_model_output

    def evaluate(self, dataset, batch_size=8, measure_enc_dir=None):
        """
        Evalulate the model against (typically) a test dataset.
        Returns average cross entropy loss.
        """
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=0)

        if measure_enc_dir is not None:
            measure_encodings_path = os.path.join(measure_enc_dir, 'measure_encodings.pkl')
            print("Getting measure encoding lookup from {}...".format(measure_encodings_path))
            with open(measure_encodings_path, 'rb') as file:
                self.measure_enc_lookup = pickle.load(file)
                print("\tSuccess!")

        self.eval()

        loss_fn = nn.CrossEntropyLoss()
        global_step = 0
        cumulative_loss = torch.zeros(1).to(self.device)
        with tqdm(dataloader, desc='Running batches', total=math.ceil(len(dataset)/batch_size)) as progbar:
            for batch in progbar:

                with torch.no_grad():

                    token_ids, measure_ids, track_ids = batch

                    token_ids = token_ids.to(self.device)

                    inputs, labels = token_ids[:, :-1], token_ids[:, 1:]
                    measure_ids, track_ids = measure_ids[:, :-1].numpy(), track_ids[:, :-1].numpy()

                    out = self.forward(inputs, measure_ids, track_ids)

                    # The class dimension needs to go in the middle for the CrossEntropyLoss
                    out = out.permute(0, 2, 1)

                    # And the labels need to be (batch, additional_dims)
                    labels = labels.permute(1, 0)

                    loss = loss_fn(out, labels)
                    progbar.set_postfix(Loss=loss.item())

                    cumulative_loss += loss

                    self.log_writer.add_scalar("loss", loss, global_step)
                    global_step += 1

        mean_loss = cumulative_loss / global_step

        self.train()

        return mean_loss

    def mask_logits(self, logits, k=None):
        if k is None:
            return logits
        else:
            values = torch.topk(logits, k)[0]
            batch_mins = values[:, -1].view(-1, 1).expand_as(logits)
            return torch.where(logits < batch_mins,
                               torch.ones_like(logits) * -1e10,
                               logits)

if __name__ == '__main__':
    model = UnconditionalLSTM(embed_dim=100, hidden_dim=100)

    token_ids = torch.tensor([[60, 10, 10, 64, 10, 10, 68, 10, 10]])

    output = model(token_ids)
