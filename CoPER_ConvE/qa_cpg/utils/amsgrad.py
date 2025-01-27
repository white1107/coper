""""AMSGrad for TensorFlow."""

from __future__ import absolute_import, division, print_function

import tensorflow as tf

from tensorflow.python.framework import ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import resource_variable_ops
from tensorflow.python.ops import state_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.training import optimizer
from tensorflow.python.training import training_ops

__all__ = ['AMSGradOptimizer']


class AMSGradOptimizer(optimizer.Optimizer):
    """Optimizer that implements the AMSGrad algorithm.
    """

    def __init__(self, learning_rate=0.001, beta1=0.9, beta2=0.999, epsilon=1e-8,
                 use_locking=False, name="AMSGrad"):
        """Construct a new AMSGrad optimizer.

        Initialization:

        ```
        m_0 <- 0 (Initialize initial 1st moment vector)
        v_0 <- 0 (Initialize initial 2nd moment vector)
        t <- 0 (Initialize timestep)
        ```

        The update rule for `variable` with gradient `g` uses an optimization
        described at the end of section2 of the paper:

        ```
        t <- t + 1
        lr_t <- learning_rate * sqrt(1 - beta2^t) / (1 - beta1^t)

        m_t <- beta1 * m_{t-1} + (1 - beta1) * g
        v_t <- beta2 * v_{t-1} + (1 - beta2) * g * g
        variable <- variable - lr_t * m_t / (sqrt(v_t) + epsilon)
        ```

        The default value of 1e-8 for epsilon might not be a good default in
        general. For example, when training an Inception network on ImageNet a
        current good choice is 1.0 or 0.1. Note that since AdamOptimizer uses the
        formulation just before Section 2.1 of the Kingma and Ba paper rather than
        the formulation in Algorithm 1, the "epsilon" referred to here is "epsilon
        hat" in the paper.

        The sparse implementation of this algorithm (used when the gradient is an
        IndexedSlices object, typically because of `tf.gather` or an embedding
        lookup in the forward pass) does apply momentum to variable slices even if
        they were not used in the forward pass (meaning they have a gradient equal
        to zero). Momentum decay (beta1) is also applied to the entire momentum
        accumulator. This means that the sparse behavior is equivalent to the dense
        behavior (in contrast to some momentum implementations which ignore momentum
        unless a variable slice was actually used).

        Args:
          learning_rate: A Tensor or a floating point value.  The learning rate.
          beta1: A float value or a constant float tensor.
            The exponential decay rate for the 1st moment estimates.
          beta2: A float value or a constant float tensor.
            The exponential decay rate for the 2nd moment estimates.
          epsilon: A small constant for numerical stability. This epsilon is
            "epsilon hat" in the Kingma and Ba paper (in the formula just before
            Section 2.1), not the epsilon in Algorithm 1 of the paper.
          use_locking: If True use locks for update operations.
          name: Optional name for the operations created when applying gradients.
            Defaults to "Adam".
        """
        super(AMSGradOptimizer, self).__init__(use_locking, name)
        self._lr = learning_rate
        self._beta1 = beta1
        self._beta2 = beta2
        self._epsilon = epsilon

        # Tensor versions of the constructor arguments, created in _prepare().
        self._lr_t = None
        self._beta1_t = None
        self._beta2_t = None
        self._epsilon_t = None

        # Variables to accumulate the powers of the beta parameters.
        # Created in _create_slots when we know the variables to optimize.
        self._beta1_power = None
        self._beta2_power = None

        # Created in SparseApply if needed.
        self._updated_lr = None

    def _get_beta_accumulators(self):
        return self._beta1_power, self._beta2_power

    def _create_slots(self, var_list):
        # Create the beta1 and beta2 accumulators on the same device as the first
        # variable. Sort the var_list to make sure this device is consistent across
        # workers (these need to go on the same PS, otherwise some updates are
        # silently ignored).
        first_var = min(var_list, key=lambda x: x.name)

        if (self._beta1_power is None or
                self._beta1_power.graph is not first_var.graph):
            with ops.colocate_with(first_var):
                self._beta1_power = variable_scope.variable(self._beta1,
                                                            name="beta1_power",
                                                            trainable=False)
                self._beta2_power = variable_scope.variable(self._beta2,
                                                            name="beta2_power",
                                                            trainable=False)
        # Create slots for the first and second moments.
        for v in var_list:
            self._zeros_slot(v, "m", self._name)
            self._zeros_slot(v, "v", self._name)
            self._zeros_slot(v, "v_hat", self._name)

    def _prepare(self):
        self._lr_t = ops.convert_to_tensor(self._lr, name="learning_rate")
        self._beta1_t = ops.convert_to_tensor(self._beta1, name="beta1")
        self._beta2_t = ops.convert_to_tensor(self._beta2, name="beta2")
        self._epsilon_t = ops.convert_to_tensor(self._epsilon, name="epsilon")

    def _resource_apply_dense(self, grad, var):
        return self._apply_dense_shared(grad, var)

    def _apply_dense_shared(self, grad, var):
        beta1_power = math_ops.cast(self._beta1_power, var.dtype.base_dtype)
        beta2_power = math_ops.cast(self._beta2_power, var.dtype.base_dtype)
        lr_t = math_ops.cast(self._lr_t, var.dtype.base_dtype)
        beta1_t = math_ops.cast(self._beta1_t, var.dtype.base_dtype)
        beta2_t = math_ops.cast(self._beta2_t, var.dtype.base_dtype)
        epsilon_t = math_ops.cast(self._epsilon_t, var.dtype.base_dtype)
        lr = (lr_t * math_ops.sqrt(1 - beta2_power) / (1 - beta1_power))

        # m_t = beta1 * m + (1 - beta1) * g_t
        m = self.get_slot(var, "m")
        m_scaled_g = grad * (1 - beta1_t)
        m_t = state_ops.assign(m, m * beta1_t, use_locking=self._use_locking)
        with ops.control_dependencies([m_t]):
            m_t = m + m_scaled_g

        # v_t = beta2 * v + (1 - beta2) * (g_t * g_t)
        v = self.get_slot(var, "v")
        v_scaled_g = (grad * grad) * (1 - beta2_t)
        v_t = state_ops.assign(v, v * beta2_t, use_locking=self._use_locking)
        with ops.control_dependencies([v_t]):
            v_t = v + v_scaled_g

        v_hat = self.get_slot(var, "v_hat")
        v_hat = v_hat.assign(tf.maximum(v_hat, v_t))
        v_sqrt = math_ops.sqrt(v_hat)
        var_update = state_ops.assign_sub(var,
                                          lr * m_t / (v_sqrt + epsilon_t),
                                          use_locking=self._use_locking)
        return control_flow_ops.group(*[var_update, m_t, v_t])

    def _apply_sparse_shared(self, grad, var, indices, scatter_add):
        beta1_power = math_ops.cast(self._beta1_power, var.dtype.base_dtype)
        beta2_power = math_ops.cast(self._beta2_power, var.dtype.base_dtype)
        lr_t = math_ops.cast(self._lr_t, var.dtype.base_dtype)
        beta1_t = math_ops.cast(self._beta1_t, var.dtype.base_dtype)
        beta2_t = math_ops.cast(self._beta2_t, var.dtype.base_dtype)
        epsilon_t = math_ops.cast(self._epsilon_t, var.dtype.base_dtype)
        lr = (lr_t * math_ops.sqrt(1 - beta2_power) / (1 - beta1_power))
        # m_t = beta1 * m + (1 - beta1) * g_t
        m = self.get_slot(var, "m")
        m_scaled_g_values = grad * (1 - beta1_t)
        m_t = state_ops.assign(m, m * beta1_t,
                               use_locking=self._use_locking)
        with ops.control_dependencies([m_t]):
            m_t = scatter_add(m, indices, m_scaled_g_values)
        # v_t = beta2 * v + (1 - beta2) * (g_t * g_t)
        v = self.get_slot(var, "v")
        v_scaled_g_values = (grad * grad) * (1 - beta2_t)
        v_t = state_ops.assign(v, v * beta2_t, use_locking=self._use_locking)
        with ops.control_dependencies([v_t]):
            v_t = scatter_add(v, indices, v_scaled_g_values)

        v_hat = self.get_slot(var, "v_hat")
        v_hat = v_hat.assign(tf.maximum(v_hat, v_t))
        v_sqrt = math_ops.sqrt(v_hat)
        var_update = state_ops.assign_sub(var,
                                          lr * m_t / (v_sqrt + epsilon_t),
                                          use_locking=self._use_locking)
        return control_flow_ops.group(*[var_update, m_t, v_t])

    def _apply_sparse(self, grad, var):
        return self._apply_sparse_shared(
            grad.values, var, grad.indices,
            lambda x, i, v: state_ops.scatter_add(  # pylint: disable=g-long-lambda
                x, i, v, use_locking=self._use_locking))

    def _resource_scatter_add(self, x, i, v):
        with ops.control_dependencies(
                [resource_variable_ops.resource_scatter_add(
                    x.handle, i, v)]):
            return x.value()

    def _resource_apply_sparse(self, grad, var, indices):
        return self._apply_sparse_shared(
            grad, var, indices, self._resource_scatter_add)

    def _resource_apply_sparse_duplicate_indices(self, grad, handle, indices):
        """Add ops to apply sparse gradients to `handle`, with repeated indices.

        Optimizers which override this method must deal with repeated indices. See
        the docstring of `_apply_sparse_duplicate_indices` for details. By default
        the correct behavior, to sum non-unique indices and their associated
        gradients, is enforced by first pre-processing `grad` and `indices` and
        passing them on to `_resource_apply_sparse`. Optimizers which deal correctly
        with duplicate indices may instead override this method to avoid the
        overhead of summing.

        Args:
          grad: a `Tensor` representing the gradient for the affected indices.
          handle: a `Tensor` of dtype `resource` which points to the variable
           to be updated.
          indices: a `Tensor` of integral type representing the indices for
           which the gradient is nonzero. Indices may be repeated.

        Returns:
          An `Operation` which updates the value of the variable.
        """
        return self._resource_apply_sparse(grad, handle, indices)

    def _finish(self, update_ops, name_scope):
        # Update the power accumulators.
        with ops.control_dependencies(update_ops):
            with ops.colocate_with(self._beta1_power):
                update_beta1 = self._beta1_power.assign(
                    self._beta1_power * self._beta1_t,
                    use_locking=self._use_locking)
                update_beta2 = self._beta2_power.assign(
                    self._beta2_power * self._beta2_t,
                    use_locking=self._use_locking)
        return control_flow_ops.group(*update_ops + [update_beta1, update_beta2],
                                      name=name_scope)
