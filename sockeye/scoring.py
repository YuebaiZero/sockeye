# Copyright 2018--2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not
# use this file except in compliance with the License. A copy of the License
# is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""
Code for scoring.
"""
import logging
import math
import time
from typing import cast, Dict, List, Optional, Union

import mxnet as mx
import numpy as np

from . import constants as C
from . import data_io
from . import inference
from . import vocab
from .model import SockeyeModel
from .beam_search import CandidateScorer
from .output_handler import OutputHandler

logger = logging.getLogger(__name__)


class BatchScorer(mx.gluon.HybridBlock):

    def __init__(self,
                 scorer: CandidateScorer,
                 score_type: str = C.SCORING_TYPE_DEFAULT,
                 constant_length_ratio: Optional[float] = None,
                 prefix='BatchScorer_',
                 softmax_temperature: Optional[float] = None) -> None:
        super().__init__(prefix=prefix)
        self.score_type = score_type
        self.scorer = scorer
        self.constant_length_ratio = constant_length_ratio
        self.softmax_temperature = softmax_temperature

    def hybrid_forward(self, F, logits, labels, length_ratio, source_length, target_length):
        """

        :param F: MXNet Namespace
        :param logits: Model logits. Shape: (batch, length, vocab_size).
        :param labels: Gold targets. Shape: (batch, length).
        :param length_ratio: Length Ratios. Shape: (batch,).
        :param source_length: Source lengths. Shape: (batch,).
        :param target_length: Target lengths. Shape: (batch,).
        :return: Sequence scores. Shape: (batch,).
        """
        logprobs = F.log_softmax(logits, axis=-1, temperature=self.softmax_temperature)

        # Select the label probability, then take their logs.
        # probs and scores: (batch_size, target_seq_len)
        token_scores = F.pick(logprobs, labels, axis=-1)
        if self.score_type == C.SCORING_TYPE_NEGLOGPROB:
            token_scores = token_scores * -1

        # Sum, then apply length penalty. The call to `mx.sym.where` masks out invalid values from scores.
        # zeros and sums: (batch_size,)
        scores = F.sum(F.where(labels != 0, token_scores, F.zeros_like(token_scores)), axis=1)

        if self.constant_length_ratio is not None and self.constant_length_ratio > 0.0:
            predicted_output_length = source_length * self.constant_length_ratio
        else:
            predicted_output_length = source_length * length_ratio

        scores = self.scorer(scores, target_length, predicted_output_length)

        return scores


class Scorer:
    """
    Scorer class takes a ScoringModel and uses it to score a stream of parallel sentences.
    It also takes the vocabularies so that the original sentences can be printed out, if desired.

    :param model: The model to score with.
    :param batch_scorer: BatchScorer block to score each batch.
    :param source_vocabs: The source vocabularies.
    :param target_vocab: The target vocabulary.
    :param context: Context.
    """
    def __init__(self,
                 model: SockeyeModel,
                 batch_scorer: BatchScorer,
                 source_vocabs: List[vocab.Vocab],
                 target_vocab: vocab.Vocab,
                 context: Union[List[mx.context.Context], mx.context.Context]) -> None:
        self.source_vocab_inv = vocab.reverse_vocab(source_vocabs[0])
        self.target_vocab_inv = vocab.reverse_vocab(target_vocab)
        self.model = model
        self.batch_scorer = batch_scorer
        self.context = context
        self.exclude_list = {C.BOS_ID, C.EOS_ID, C.PAD_ID}

    def score_batch(self, batch: data_io.Batch) -> mx.nd.NDArray:
        batch = batch.split_and_load(ctx=self.context)
        batch_scores = []  # type: List[mx.nd.NDArray]
        for inputs, labels in batch.shards():
            source, source_length, target, target_length = inputs
            outputs = self.model(*inputs)  # type: Dict[str, mx.nd.NDArray]
            logits = outputs[C.LOGITS_NAME]  # type: mx.nd.NDArray
            label = labels[C.TARGET_LABEL_NAME]
            length_ratio = outputs.get(C.LENRATIO_NAME, mx.nd.zeros_like(source_length))
            scores = self.batch_scorer(logits, label, length_ratio, source_length, target_length)
            batch_scores.append(scores)

        # shape: (batch_size,).
        batch_scores = mx.nd.concat(*batch_scores, dim=0)
        return cast(mx.nd.NDArray, batch_scores)

    def score(self, score_iter: data_io.BaseParallelSampleIter, output_handler: OutputHandler):
        total_time = 0.
        sentence_no = 0
        batch_no = 0
        for batch_no, batch in enumerate(score_iter, 1):
            batch_tic = time.time()
            scores = self.score_batch(batch)
            batch_time = time.time() - batch_tic
            total_time += batch_time

            for sentno, (source, target, score) in enumerate(zip(batch.source.astype('int32')[:, :, 0].asnumpy(),
                                                                 batch.target.astype('int32').asnumpy(),
                                                                 scores.asnumpy()), 1):
                sentence_no += 1

                # Transform arguments in preparation for printing
                source_ids = source.tolist()
                source_tokens = list(data_io.ids2tokens(source_ids, self.source_vocab_inv, self.exclude_list))
                target_ids = target.tolist()
                target_string = C.TOKEN_SEPARATOR.join(
                    data_io.ids2tokens(target_ids, self.target_vocab_inv, self.exclude_list))

                # Report a score of -inf for invalid sentence pairs (empty source and/or target)
                if source[0] == C.PAD_ID or target[0] == C.PAD_ID:
                    score = -np.inf

                # Output handling routines require us to make use of inference classes.
                output_handler.handle(inference.TranslatorInput(sentence_no, source_tokens),
                                      inference.TranslatorOutput(sentence_no, target_string, None, score),
                                      batch_time)

        if sentence_no != 0:
            logger.info("Processed %d lines in %d batches. Total time: %.4f, sec/sent: %.4f, sent/sec: %.4f",
                        sentence_no, math.ceil(sentence_no / batch_no), total_time,
                        total_time / sentence_no, sentence_no / total_time)
        else:
            logger.info("Processed 0 lines.")
