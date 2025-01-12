# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for matmul."""

import copy
from unittest import mock

from absl.testing import absltest
from absl.testing import parameterized
from aqt.common import aqt_config
from aqt.tensorflow import aqt_matmul
from aqt.tensorflow import aqt_tensor
from aqt.test import aqt_matmul_test_base
import numpy as np
import tensorflow.compat.v1 as tf


def update_event_count(matmul, event_count_int: int):
  """Update the quantizer's event count without changing stats."""
  for quantizer in [matmul.lhs_quantizer, matmul.rhs_quantizer]:
    sample = tf.zeros(quantizer.data_shape)
    weights = tf.zeros([1] * len(quantizer.data_shape))
    event_count = tf.constant(event_count_int, tf.int64)
    quantizer.update(sample, weights, event_count).run()


def matmul_config(matmul):
  """Creates an AqtMatmulConfig corresponding to a Matmul."""
  return aqt_config.AqtMatmulConfig(matmul.lhs_quantizer.config,
                                    matmul.rhs_quantizer.config)


class IntNarrowedMatMulTest(tf.test.TestCase, parameterized.TestCase):

  def test_chooses_right_matmul(self):
    # Create a list of settings (left_bits, right_bits, expected_matmul)
    # and generate a schedule based on those settings.
    settings = [(8, 16, "default"), (4, 16, "default"), (4, 8, "int8"),
                (8, 9, "default"), (4, 4, "int8"), (3, 3, "int8"),
                (9, 7, "default")]

    lhs_schedule = []
    rhs_schedule = []
    expected_results = []
    for i, (l, r, expected) in enumerate(settings):
      lhs_schedule.append((i, i + 1, l))
      rhs_schedule.append((i, i + 1, r))
      expected_results.append(expected)

    lhs_config = aqt_matmul_test_base.config_from_schedule(lhs_schedule)
    rhs_config = aqt_matmul_test_base.config_from_schedule(rhs_schedule)

    shape = [1, 1]  # Any shape will do, we're mocking.
    lhs_quant = aqt_tensor.TensorQuantizer(shape, lhs_config, name="lhs")
    rhs_quant = aqt_tensor.TensorQuantizer(shape, rhs_config, name="rhs")

    module = "aqt.tensorflow.aqt_matmul"
    with mock.patch(f"{module}.default_matmul") as default_matmul, \
         mock.patch(f"{module}.int8_matmul") as int8_matmul:
      default_matmul.return_value = tf.constant("default")
      int8_matmul.return_value = tf.constant("int8")

      event_ph = tf.placeholder(tf.int64)
      lhs_quant._last_update = event_ph
      rhs_quant._last_update = event_ph
      tf_actual = aqt_matmul._matmul_case(lhs_quant, rhs_quant, None, None,
                                          True)

      with self.cached_session():
        tf.global_variables_initializer().run()
        for i, expected in enumerate(expected_results):
          actual = tf_actual.eval(feed_dict={event_ph: i})
          self.assertEqual(
              actual.decode("utf-8"), expected, msg=f"event_count {i}")


class MatmulTest(aqt_matmul_test_base.MatmulTest):

  def constant(self, x):
    return tf.constant(x, dtype=tf.float32)

  def matmul(self, config, lhs_shape, rhs_shape, name="aqt"):
    return aqt_matmul.Matmul(config, lhs_shape, rhs_shape, name)

  def matmul_apply(self, mm, lhs, rhs, train=True, keep_stats=False):
    event_count = tf.constant(0, tf.int64)
    lhs_sample = tf.zeros_like(lhs) if keep_stats else lhs
    lhs_weight = tf.ones_like(lhs) if keep_stats else None
    rhs_sample = tf.zeros_like(rhs) if keep_stats else rhs
    rhs_weight = tf.ones_like(rhs) if keep_stats else None
    updates = [
        mm.update_lhs(lhs_sample, lhs_weight, event_count),
        mm.update_rhs(rhs_sample, rhs_weight, event_count)
    ]
    with tf.control_dependencies(updates):
      result = mm.apply(lhs, rhs, train=train)

    with self.cached_session() as sess, sess.as_default():
      tf.global_variables_initializer().run()
      return result.eval()

  def matmul_unquantized(self, lhs, rhs):
    result = tf.matmul(lhs, rhs)
    with self.cached_session() as sess, sess.as_default():
      tf.global_variables_initializer().run()
      return result.eval()

  def gradients(self, fwd_func, x, w, use_reduce=False):
    if use_reduce:
      fwd_func = lambda x, w: tf.reduce_sum(fwd_func(x, w)**2)
    fwd = fwd_func(x, w)
    return tf.gradients([fwd], [x, w])

  def with_config(self, mm, config):
    """Returns new Matmul with the new config but otherwise the same."""
    with tf.variable_scope(None, default_name="uniqued"):
      return aqt_matmul.Matmul(config, mm.lhs_quantizer.data_shape,
                               mm.rhs_quantizer.data_shape, mm.name,
                               mm.lhs_name, mm.rhs_name)

  def test_validates_contraction(self):
    mm, _, _ = self.exact_int8_matmul_example()

    config = copy.deepcopy(matmul_config(mm))
    config.rhs.stats_config.share_stats_axes = [1]
    with self.assertRaisesRegex(aqt_config.ConfigError,
                                "expected rhs matmul contraction axis"):
      self.with_config(mm, config)

    config = copy.deepcopy(matmul_config(mm))
    config.lhs.stats_config.share_stats_axes = [0]
    with self.assertRaisesRegex(aqt_config.ConfigError,
                                "expected lhs matmul contraction axis"):
      self.with_config(mm, config)

  def test_validates_rank2(self):
    mm, lhs, rhs = self.exact_int8_matmul_example()

    mm.rhs_quantizer.data_shape.append(1)
    with self.assertRaisesRegex(aqt_config.ConfigError, "rhs data shape"):
      mm.apply(lhs, rhs)
    mm.rhs_quantizer.data_shape = mm.rhs_quantizer.data_shape[:-1]

    mm.lhs_quantizer.data_shape += (1,)
    with self.assertRaisesRegex(aqt_config.ConfigError, "lhs data shape"):
      mm.apply(lhs, rhs)

  def test_grad_linearity(self):
    """Validates gradients are correct on basic example."""
    float_config_tc = aqt_config.AqtTensorConfig(
        freeze_scale_at_begin=True,
        quant_config=aqt_config.FloatConfig(),
        calibration_config=aqt_matmul_test_base.calibration_config(1))
    float_config = aqt_config.AqtScheduleConfig(
        aqt_matmul_test_base.test_stats_config(), [float_config_tc])
    scale = 10.0
    int_config = aqt_matmul_test_base._schedule_config(8, scale, (0, 1))

    lhs_config, rhs_config = int_config, float_config
    contract_dim = 10
    lhs_shape = (1, contract_dim)
    rhs_shape = (contract_dim, 1)
    target_shape = lhs_shape[:1] + rhs_shape[1:]

    lhs_ph = tf.placeholder(tf.float32, shape=lhs_shape)
    rhs_ph = tf.placeholder(tf.float32, shape=rhs_shape)
    target_ph = tf.placeholder(tf.float32, shape=target_shape)

    config = aqt_config.AqtMatmulConfig(lhs_config, rhs_config)
    mm = aqt_matmul.Matmul(config, lhs_shape, rhs_shape)

    with self.cached_session() as sess, sess.as_default():
      tf.global_variables_initializer().run()

      event_count = tf.constant(0, tf.int64)
      updates = [
          mm.update_lhs(tf.ones(lhs_shape), None, event_count),
          mm.update_rhs(tf.ones(rhs_shape), None, event_count)
      ]
      with tf.control_dependencies(updates):
        aqt_mm = mm.apply(lhs_ph, rhs_ph)

      aqt_diff = aqt_mm - target_ph
      aqt_loss = tf.reduce_sum(aqt_diff**2) / 2
      aqt_mm_grad = tf.gradients([aqt_loss], [rhs_ph])[0]

      rng = np.random.default_rng(1234)
      for i in range(10):
        lhs = rng.standard_normal(lhs_shape).astype(np.float32)
        rhs = rng.standard_normal(rhs_shape).astype(np.float32)
        target = rng.standard_normal(target_shape).astype(np.float32)

        feed_dict = {lhs_ph: lhs, rhs_ph: rhs, target_ph: target}

        aqtd, aqt_grad = sess.run([aqt_diff, aqt_mm_grad], feed_dict=feed_dict)

        # Notice aqt gradient at position i is quantized(lhs)[i] * aqtd
        # assuming linearity of gradients.
        grad_factor = aqtd.ravel()
        float_grad = lhs.ravel() * grad_factor
        true_grad = aqt_grad.ravel()
        diff = np.abs(float_grad - true_grad)
        bucket_width = scale * 2 / 255
        for j, err in enumerate(diff):
          self.assertLessEqual(
              err,
              bucket_width * abs(grad_factor),
              msg=f"trial {i} position {j}")

  def test_diagnostics(self):
    mm, lhs, rhs = self.exact_int8_matmul_example()

    with self.cached_session():
      tf.global_variables_initializer().run()
      update_event_count(mm, 0)

      d = mm.diagnostics(lhs, rhs)
      quantizers = {"lhs": mm.lhs_quantizer, "rhs": mm.rhs_quantizer}
      for qname, quantizer in quantizers.items():

        for name, expected in quantizer.calibration_variables().items():
          actual = d[f"aqt/{qname}/{name}"]
          self.assertAllEqual(actual, expected)

        actual = d[f"aqt/{qname}/clipped_proportion"]
        expected = 0.0
        self.assertAllEqual(actual, expected)

        actual = d[f"aqt/{qname}/clip"]
        expected = quantizer.clip_range()
        self.assertAllEqual(actual, expected)

      out_of_range_lhs, out_of_range_rhs = (
          tf.ones_like(x) * 512.0 for x in (lhs, rhs))
      d = mm.diagnostics(out_of_range_lhs, out_of_range_rhs)
      for arg in ["lhs", "rhs"]:
        actual = d[f"aqt/{arg}/clipped_proportion"]
        expected = 1.0
        self.assertAllEqual(actual, expected)


if __name__ == "__main__":
  absltest.main()
