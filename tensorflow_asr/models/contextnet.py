# Copyright 2020 Huy Le Nguyen (@usimarit)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Ref: https://github.com/iankur/ContextNet """

from typing import List
import tensorflow as tf
from .transducer import Transducer
from ..utils.utils import merge_two_last_dims, get_reduced_length

L2 = tf.keras.regularizers.l2(1e-6)


def get_activation(activation: str = "silu"):
    activation = activation.lower()
    if activation in ["silu", "swish"]: return tf.nn.swish
    elif activation == "relu": return tf.nn.relu
    elif activation == "linear": return tf.keras.activations.linear
    else: raise ValueError("activation must be either 'silu', 'swish', 'relu' or 'linear'")


class Reshape(tf.keras.layers.Layer):
    def call(self, inputs): return merge_two_last_dims(inputs)


class ConvModule(tf.keras.layers.Layer):
    def __init__(self,
                 kernel_size: int = 3,
                 strides: int = 1,
                 filters: int = 256,
                 activation: str = "silu",
                 kernel_regularizer = None,
                 bias_regularizer = None,
                 **kwargs):
        super(ConvModule, self).__init__(**kwargs)
        self.strides = strides
        self.conv = tf.keras.layers.SeparableConv1D(
            filters=filters, kernel_size=kernel_size, strides=strides, padding="same",
            depthwise_regularizer=kernel_regularizer, pointwise_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer, name=f"{self.name}_conv"
        )
        self.bn = tf.keras.layers.BatchNormalization(name=f"{self.name}_bn")
        self.activation = get_activation(activation)

    def call(self, inputs, training=False, **kwargs):
        outputs = self.conv(inputs, training=training)
        outputs = self.bn(outputs, training=training)
        outputs = self.activation(outputs)
        return outputs


class SEModule(tf.keras.layers.Layer):
    def __init__(self,
                 kernel_size: int = 3,
                 strides: int = 1,
                 filters: int = 256,
                 activation: str = "silu",
                 kernel_regularizer = None,
                 bias_regularizer = None,
                 **kwargs):
        super(SEModule, self).__init__(**kwargs)
        self.conv = ConvModule(
            kernel_size=kernel_size, strides=strides,
            filters=filters, activation=activation,
            kernel_regularizer=kernel_regularizer, bias_regularizer=bias_regularizer,
            name=f"{self.name}_conv_module"
        )
        self.activation = get_activation(activation)
        self.fc1 = tf.keras.layers.Dense(filters // 8, name=f"{self.name}_fc1")
        self.fc2 = tf.keras.layers.Dense(filters, name=f"{self.name}_fc2")

    def call(self, inputs, training=False, **kwargs):
        features, input_length = inputs
        outputs = self.conv(features, training=training)

        se = tf.divide(tf.reduce_sum(outputs, axis=1), tf.expand_dims(tf.cast(input_length, dtype=outputs.dtype), axis=1))
        se = self.fc1(se, training=training)
        se = self.activation(se)
        se = self.fc2(se, training=training)
        se = self.activation(se)
        se = tf.nn.sigmoid(se)
        se = tf.expand_dims(se, axis=1)

        outputs = tf.multiply(outputs, se)
        return outputs


class ConvBlock(tf.keras.layers.Layer):
    def __init__(self,
                 nlayers: int = 3,
                 kernel_size: int = 3,
                 filters: int = 256,
                 strides: int = 1,
                 residual: bool = True,
                 activation: str = 'silu',
                 alpha: float = 1.0,
                 kernel_regularizer = None,
                 bias_regularizer = None,
                 **kwargs):
        super(ConvBlock, self).__init__(**kwargs)

        self.dmodel = filters
        self.time_reduction_factor = strides
        filters = int(filters * alpha)

        self.convs = []
        for i in range(nlayers - 1):
            self.convs.append(
                ConvModule(
                    kernel_size=kernel_size, strides=1,
                    filters=filters, activation=activation,
                    kernel_regularizer=kernel_regularizer, bias_regularizer=bias_regularizer,
                    name=f"{self.name}_conv_module_{i}"
                )
            )

        self.last_conv = ConvModule(
            kernel_size=kernel_size, strides=strides,
            filters=filters, activation=activation,
            kernel_regularizer=kernel_regularizer, bias_regularizer=bias_regularizer,
            name=f"{self.name}_conv_module_{nlayers - 1}"
        )

        self.se = SEModule(
            kernel_size=kernel_size, strides=1, filters=filters, activation=activation,
            kernel_regularizer=kernel_regularizer, bias_regularizer=bias_regularizer,
            name=f"{self.name}_se"
        )

        self.residual = None
        if residual:
            self.residual = ConvModule(
                kernel_size=kernel_size, strides=strides,
                filters=filters, activation="linear",
                kernel_regularizer=kernel_regularizer, bias_regularizer=bias_regularizer,
                name=f"{self.name}_residual"
            )

        self.activation = get_activation(activation)

    def call(self, inputs, training=False, **kwargs):
        features, input_length = inputs
        outputs = features
        for conv in self.convs:
            outputs = conv(outputs, training=training)
        outputs = self.last_conv(outputs, training=training)
        input_length = get_reduced_length(input_length, self.last_conv.strides)
        outputs = self.se([outputs, input_length], training=training)
        if self.residual is not None:
            res = self.residual(features, training=training)
            outputs = tf.add(outputs, res)
        outputs = self.activation(outputs)
        return outputs, input_length


class ContextNetEncoder(tf.keras.Model):
    def __init__(self,
                 blocks: List[dict] = [],
                 alpha: float = 1.0,
                 kernel_regularizer = None,
                 bias_regularizer = None,
                 **kwargs):
        super(ContextNetEncoder, self).__init__(**kwargs)

        self.reshape = Reshape(name=f"{self.name}_reshape")

        self.blocks = []
        for i, config in enumerate(blocks):
            self.blocks.append(
                ConvBlock(
                    **config, alpha=alpha,
                    kernel_regularizer=kernel_regularizer, bias_regularizer=bias_regularizer,
                    name=f"{self.name}_block_{i}"
                )
            )

    def call(self, inputs, training=False, **kwargs):
        outputs, input_length = inputs
        outputs = self.reshape(outputs)
        for block in self.blocks:
            outputs, input_length = block([outputs, input_length], training=training)
        return outputs


class ContextNet(Transducer):
    def __init__(self,
                 vocabulary_size: int,
                 encoder_blocks: List[dict],
                 encoder_alpha: float = 0.5,
                 prediction_embed_dim: int = 512,
                 prediction_embed_dropout: int = 0,
                 prediction_num_rnns: int = 1,
                 prediction_rnn_units: int = 320,
                 prediction_rnn_type: str = "lstm",
                 prediction_rnn_implementation: int = 2,
                 prediction_layer_norm: bool = True,
                 prediction_projection_units: int = 0,
                 joint_dim: int = 1024,
                 joint_activation: str = "tanh",
                 kernel_regularizer=L2,
                 bias_regularizer=L2,
                 name: str = "contextnet",
                 **kwargs):
        super(ContextNet, self).__init__(
            encoder=ContextNetEncoder(
                blocks=encoder_blocks,
                alpha=encoder_alpha,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer,
                name=f"{name}_encoder"
            ),
            vocabulary_size=vocabulary_size,
            embed_dim=prediction_embed_dim,
            embed_dropout=prediction_embed_dropout,
            num_rnns=prediction_num_rnns,
            rnn_units=prediction_rnn_units,
            rnn_type=prediction_rnn_type,
            rnn_implementation=prediction_rnn_implementation,
            layer_norm=prediction_layer_norm,
            projection_units=prediction_projection_units,
            joint_dim=joint_dim,
            joint_activation=joint_activation,
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer,
            name=name, **kwargs
        )
        self.dmodel = self.encoder.blocks[-1].dmodel
        self.time_reduction_factor = 1
        for block in self.encoder.blocks: self.time_reduction_factor *= block.time_reduction_factor

    def call(self, inputs, training=False, **kwargs):
        features, input_length, prediction, prediction_length = inputs
        enc = self.encoder([features, input_length], training=training, **kwargs)
        pred = self.predict_net([prediction, prediction_length], training=training, **kwargs)
        outputs = self.joint_net([enc, pred], training=training, **kwargs)
        return outputs

    def encoder_inference(self, features: tf.Tensor, input_length: tf.Tensor):
        with tf.name_scope(f"{self.name}_encoder"):
            input_length = tf.expand_dims(tf.shape(features)[0], axis=0)
            outputs = tf.expand_dims(features, axis=0)
            outputs = self.encoder([outputs, input_length], training=False)
            return tf.squeeze(outputs, axis=0)

    # -------------------------------- GREEDY -------------------------------------

    @tf.function
    def recognize(self,
                  features: tf.Tensor,
                  input_length: tf.Tensor,
                  parallel_iterations: int = 10,
                  swap_memory: bool = True):
        """
        RNN Transducer Greedy decoding
        Args:
            features (tf.Tensor): a batch of padded extracted features

        Returns:
            tf.Tensor: a batch of decoded transcripts
        """
        encoded = self.encoder([features, input_length], training=False)
        return self._perform_greedy_batch(encoded, input_length,
                                          parallel_iterations=parallel_iterations, swap_memory=swap_memory)

    def recognize_tflite(self, signal, predicted, prediction_states):
        """
        Function to convert to tflite using greedy decoding (default streaming mode)
        Args:
            signal: tf.Tensor with shape [None] indicating a single audio signal
            predicted: last predicted character with shape []
            prediction_states: lastest prediction states with shape [num_rnns, 1 or 2, 1, P]

        Return:
            transcript: tf.Tensor of Unicode Code Points with shape [None] and dtype tf.int32
            predicted: last predicted character with shape []
            encoder_states: lastest encoder states with shape [num_rnns, 1 or 2, 1, P]
            prediction_states: lastest prediction states with shape [num_rnns, 1 or 2, 1, P]
        """
        features = self.speech_featurizer.tf_extract(signal)
        encoded = self.encoder_inference(features, tf.shape(features)[0])
        hypothesis = self._perform_greedy(encoded, tf.shape(encoded)[0], predicted, prediction_states)
        transcript = self.text_featurizer.indices2upoints(hypothesis.prediction)
        return transcript, hypothesis.index, hypothesis.states

    def recognize_tflite_with_timestamp(self, signal, predicted, states):
        features = self.speech_featurizer.tf_extract(signal)
        encoded = self.encoder_inference(features, tf.shape(features)[0])
        hypothesis = self._perform_greedy(encoded, tf.shape(encoded)[0], predicted, states)
        indices = self.text_featurizer.normalize_indices(hypothesis.prediction)
        upoints = tf.gather_nd(self.text_featurizer.upoints, tf.expand_dims(indices, axis=-1))  # [None, max_subword_length]

        num_samples = tf.cast(tf.shape(signal)[0], dtype=tf.float32)
        total_time_reduction_factor = self.time_reduction_factor * self.speech_featurizer.frame_step

        stime = tf.range(0, num_samples, delta=total_time_reduction_factor, dtype=tf.float32)
        stime /= tf.cast(self.speech_featurizer.sample_rate, dtype=tf.float32)

        etime = tf.range(total_time_reduction_factor, num_samples, delta=total_time_reduction_factor, dtype=tf.float32)
        etime /= tf.cast(self.speech_featurizer.sample_rate, dtype=tf.float32)

        non_blank = tf.where(tf.not_equal(upoints, 0))
        non_blank_transcript = tf.gather_nd(upoints, non_blank)
        non_blank_stime = tf.gather_nd(tf.repeat(tf.expand_dims(stime, axis=-1), tf.shape(upoints)[-1], axis=-1), non_blank)
        non_blank_etime = tf.gather_nd(tf.repeat(tf.expand_dims(etime, axis=-1), tf.shape(upoints)[-1], axis=-1), non_blank)

        return non_blank_transcript, non_blank_stime, non_blank_etime, hypothesis.index, hypothesis.states

    # -------------------------------- BEAM SEARCH -------------------------------------

    @tf.function
    def recognize_beam(self,
                       features: tf.Tensor,
                       input_length: tf.Tensor,
                       lm: bool = False,
                       parallel_iterations: int = 10,
                       swap_memory: bool = True):
        """
        RNN Transducer Beam Search
        Args:
            features (tf.Tensor): a batch of padded extracted features
            lm (bool, optional): whether to use language model. Defaults to False.

        Returns:
            tf.Tensor: a batch of decoded transcripts
        """
        encoded = self.encoder([features, input_length], training=False)
        return self._perform_beam_search_batch(encoded, input_length, lm,
                                               parallel_iterations=parallel_iterations, swap_memory=swap_memory)
