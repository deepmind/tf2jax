# Copyright 2022 DeepMind Technologies Limited. All Rights Reserved.
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
# ==============================================================================
"""Tests for tf2jax."""

import contextlib
import itertools

from typing import Tuple

from absl.testing import parameterized

import chex
import jax
import numpy as np

import tensorflow as tf
from tf2jax._src import tf2jax
import tree


def _parse_version(version: str) -> Tuple[int, ...]:
  return tuple([int(v) for v in version.split(".")])


class OpsTest(tf.test.TestCase, parameterized.TestCase):

  def _test_convert(self,
                    tf_func,
                    inputs,
                    *,
                    check_shape_only=False,
                    functional=True,
                    tf_on_cpu=False):
    if not isinstance(inputs, (list, tuple)):
      inputs = (inputs,)

    tf_context = tf.device("/cpu:0") if tf_on_cpu else contextlib.nullcontext()

    with tf_context:
      jax_func, jax_params = tf2jax.convert(
          tf.function(tf_func), *tree.map_structure(np.zeros_like, inputs))
    if functional:
      self.assertEmpty(jax_params, "Expected no parameters for pure Ops.")

    jax_func = self.variant(jax_func)

    rng = jax.random.PRNGKey(42)
    jax_results, new_jax_params = jax_func(jax_params, *inputs, rng=rng)
    with tf_context:
      tf_results = tf_func(*inputs)

    # Check outputs
    for tf_res, jax_res in zip(
        tree.flatten(tf_results), tree.flatten(jax_results)):
      self.assertEqual(tf_res.shape, jax_res.shape)
      if not check_shape_only:
        self.assertAllClose(np.asarray(tf_res), jax_res, atol=1e-5)

    return jax_results, new_jax_params

  @chex.variants(with_jit=True, without_jit=True)
  def test_cholesky(self):
    np.random.seed(42)
    inputs = np.stack([
        np.cov(np.random.normal(size=[5, 10]).astype(np.float32))
        for _ in range(3)
    ])
    self._test_convert(
        lambda x: tf.raw_ops.Cholesky(input=x), [inputs], tf_on_cpu=True)

  @chex.variants(with_jit=True, without_jit=True)
  @parameterized.named_parameters(
      chex.params_product(
          (("compute_v", True), ("not_compute_v", False)),
          (
              ("unbatched", (5, 5)),
              ("batched", (3, 5, 5)),
              ("more_batched", (2, 3, 5, 5)),
          ),
          named=True,
      ))
  def test_eig(self, compute_v, shape):
    if jax.default_backend().lower() != "cpu":
      self.skipTest("JAX only supports nonsymmetric eigendecomposition on CPU.")

    np.random.seed(42)
    inputs = np.random.normal(size=shape).astype(np.float32)

    def eig(x):
      return tf.raw_ops.Eig(input=x, compute_v=compute_v, Tout=tf.complex64)

    tf_w, tf_v = eig(tf.constant(inputs))

    jax_eig = tf2jax.convert_functional(tf.function(eig), np.zeros_like(inputs))
    jax_w, jax_v = self.variant(jax_eig)(inputs)

    # Check eigenvalues are sorted in non-descending order.
    # TODO(b/239787164) Temporarily disabled.
    # self.assertTrue(
    #     np.all((np.absolute(tf_w)[..., :-1] -
    #             np.absolute(tf_w)[..., 1:]) < 1e-5))
    # self.assertTrue(
    #     np.all((np.absolute(jax_w)[..., :-1] -
    #             np.absolute(jax_w)[..., 1:]) < 1e-5))

    # Check eigenvalues so because ordering is not consistent even after sorting
    for idx in itertools.product(*map(range, shape[:-2])):
      w_jax, w_tf = jax_w[idx], tf_w[idx]
      for i in range(shape[-1]):
        self.assertAllClose(min(abs(w_jax - w_tf[i])), 0., atol=1e-5)
        self.assertAllClose(min(abs(w_jax[i] - w_tf)), 0., atol=1e-5)

    if compute_v:
      self.assertAllClose(np.matmul(inputs, tf_v), tf_w[..., None, :] * tf_v)
      self.assertAllClose(np.matmul(inputs, jax_v), jax_w[..., None, :] * jax_v)

  @chex.variants(with_jit=True, without_jit=True)
  @parameterized.named_parameters(
      chex.params_product(
          (("compute_v", True), ("not_compute_v", False)),
          (
              ("unbatched", (5, 5)),
              ("batched", (3, 5, 5)),
              ("more_batched", (2, 3, 5, 5)),
          ),
          named=True,
      ))
  def test_eigh(self, compute_v, shape):
    np.random.seed(42)
    inputs = np.random.normal(size=shape).astype(np.float32)

    def eigh(x):
      return tf.raw_ops.SelfAdjointEigV2(input=x, compute_v=compute_v)

    with tf.device("/cpu:0"):
      tf_w, tf_v = eigh(tf.constant(inputs))

      jax_eigh = tf2jax.convert_functional(
          tf.function(eigh), np.zeros_like(inputs))
    jax_w, jax_v = self.variant(jax_eigh)(inputs)

    self.assertTrue(np.all(tf_w[..., :-1] <= tf_w[..., 1:]))
    self.assertTrue(np.all(jax_w[..., :-1] <= jax_w[..., 1:]))

    # Check eigenvalues so because ordering is not consistent even after sorting
    for idx in itertools.product(*map(range, shape[:-2])):
      w_jax, w_tf = jax_w[idx], tf_w[idx]
      for i in range(shape[-1]):
        self.assertAllClose(min(abs(w_jax - w_tf[i])), 0., atol=1e-5)
        self.assertAllClose(min(abs(w_jax[i] - w_tf)), 0., atol=1e-5)

    if compute_v:
      sym_inputs = np.tril(inputs) + np.swapaxes(np.tril(inputs, -1), -2, -1)
      self.assertAllClose(
          np.matmul(sym_inputs, tf_v), tf_w[..., None, :] * tf_v)
      self.assertAllClose(
          np.matmul(sym_inputs, jax_v), jax_w[..., None, :] * jax_v)

  @chex.variants(with_jit=True, without_jit=True)
  @parameterized.named_parameters(
      chex.params_product(
          (("compute_uv", True), ("not_compute_uv", False)),
          (
              ("full_matrices", True),
              ("not_full_matrices", False),
          ),
          (
              ("unbatched", (3, 3)),
              ("batched", (3, 4, 5)),
              ("more_batched", (2, 3, 5, 4)),
          ),
          named=True,
      ))
  def test_svd(self, compute_uv, full_matrices, shape):
    np.random.seed(42)
    inputs = np.random.normal(size=shape).astype(np.float32)

    def svd(x):
      return tf.raw_ops.Svd(
          input=x, compute_uv=compute_uv, full_matrices=full_matrices)

    tf_s, tf_u, tf_v = svd(tf.constant(inputs))

    jax_svd = tf2jax.convert_functional(tf.function(svd), np.zeros_like(inputs))
    jax_s, jax_u, jax_v = self.variant(jax_svd)(inputs)

    self.assertEqual(tf_s.shape, jax_s.shape)
    if compute_uv:
      self.assertEqual(tf_u.shape, jax_u.shape)
      self.assertEqual(tf_v.shape, jax_v.shape)

    # https://numpy.org/doc/stable/reference/generated/numpy.linalg.svd.html)
    def reconstruct(u, s, v):
      return tf.matmul(
          u[..., :s.shape[-1]] * s[..., None, :],
          v[..., :s.shape[-1]],
          adjoint_b=True)

    # Adapted from tensorflow/python/kernel_tests/linalg/svd_op_test.py
    def compare_singular_vectors(x, y):
      # Singular vectors are only unique up to sign (complex phase factor for
      # complex matrices), so we normalize the sign first.
      sum_of_ratios = np.sum(np.divide(y, x), -2, keepdims=True)
      phases = np.divide(sum_of_ratios, np.abs(sum_of_ratios))
      x = x * phases
      self.assertAllClose(x, y, 1e-4)

    self.assertAllClose(jax_s, tf_s)
    if compute_uv:
      tf_recon = reconstruct(tf_u, tf_s, tf_v)
      jax_recon = reconstruct(jax_u, jax_s, jax_v)
      self.assertAllClose(tf_recon, jax_recon, atol=1e-5)
      self.assertAllClose(inputs, tf_recon, atol=1e-5)
      self.assertAllClose(inputs, jax_recon, atol=1e-5)

      rank = min(inputs.shape[-2:])
      compare_singular_vectors(tf_u[..., :rank], jax_u[..., :rank])
      compare_singular_vectors(tf_v[..., :rank], jax_v[..., :rank])


if __name__ == "__main__":
  tf.test.main()
