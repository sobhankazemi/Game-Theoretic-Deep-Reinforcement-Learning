# Copyright 2018 DeepMind Technologies Limited. All rights reserved.
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

"""DDPG learner implementation."""

import time
from typing import Dict, Iterator, List, Optional, Union, Sequence

import acme
from acme import types
from acme.tf import losses
from acme.tf import networks as acme_nets
from acme.tf import savers as tf2_savers
from acme.tf import utils as tf2_utils
from acme.utils import counting
from acme.utils import loggers
import numpy as np
import reverb
import sonnet as snt
import tensorflow as tf
import tree

Replicator = Union[snt.distribute.Replicator, snt.distribute.TpuReplicator]


class DDPGLearner(acme.Learner):
    """DDPG learner.

    This is the learning component of a DDPG agent. IE it takes a dataset as input
    and implements update functionality to learn from this dataset.
    """
    def __init__(
        self,
        agent_number: int,
        agent_action_size: int,
        
        online_networks,
        target_networks,
        # policy_network: snt.Module,
        # critic_network: snt.Module,
        # target_policy_network: snt.Module,
        # target_critic_network: snt.Module,
        discount: float,
        target_update_period: int,
        dataset_iterator: Iterator[reverb.ReplaySample],
        replicator: Optional[Replicator] = None,
        # observation_network: types.TensorTransformation = lambda x: x,
        # target_observation_network: types.TensorTransformation = lambda x: x,
        policy_optimizer: Optional[List[snt.Optimizer]] = None,
        critic_optimizer: Optional[List[snt.Optimizer]] = None,
        clipping: bool = True,
        counter: Optional[counting.Counter] = None,
        logger: Optional[loggers.Logger] = None,
        checkpoint: bool = True,
    ):
        """Initializes the learner.

        Args:
        policy_netwDocumentsork: the online (optimized) policy.
        critic_network: the online critic.
        target_policy_network: the target policy (which lags behind the online
            policy).
        target_critic_network: the target critic.
        discount: discount to use for TD updates.
        target_update_period: number of learner steps to perform before updating
            the target networks.
        dataset_iterator: dataset to learn from, whether fixed or from a replay
            buffer (see `acme.datasets.reverb.make_reverb_dataset` documentation).
        replicator: Replicates variables and their update methods over multiple
            accelerators, such as the multiple chips in a TPU.
        observation_network: an optional online network to process observations
            before the policy and the critic.
        target_observation_network: the target observation network.
        policy_optimizer: the optimizer to be applied to the DPG (policy) loss.
        critic_optimizer: the optimizer to be applied to the distributional
            Bellman loss.
        clipping: whether to clip gradients by global norm.
        counter: counter object used to keep track of steps.
        logger: logger object to be used by learner.
        checkpoint: boolean indicating whether to checkpoint the learner.
        """

        # Store online and target networks.
        self._policy_networks = [online_network.policy_network for online_network in online_networks]
        self._critic_networks = [online_network.critic_network for online_network in online_networks]
        self._target_policy_networks = [target_network.policy_network for target_network in target_networks]
        self._target_critic_networks = [target_network.critic_network for target_network in target_networks]

        # Make sure observation networks are snt.Module's so they have variables.
        self._observation_networks = [tf2_utils.to_sonnet_module(online_network.observation_network) for online_network in online_networks]
        self._target_observation_networks = [tf2_utils.to_sonnet_module(
            target_network.observation_network) for target_network in target_networks]

        # General learner book-keeping and loggers.
        self._counter = counter or counting.Counter()
        self._logger = logger or loggers.make_default_logger('learner')

        # Other learner parameters.
        self._discount = discount
        self._clipping = clipping
        
        self._agent_number = agent_number
        self._agent_action_size = agent_action_size

        # Replicates Variables across multiple accelerators
        if not replicator:
            accelerator = _get_first_available_accelerator_type()
            if accelerator == 'TPU':
                replicator = snt.distribute.TpuReplicator()
            else:
                replicator = snt.distribute.Replicator()

        self._replicator = replicator

        with replicator.scope():
            # Necessary to track when to update target networks.
            self._num_steps = tf.Variable(0, dtype=tf.int32)
            self._target_update_period = target_update_period

            # Create optimizers if they aren't given.
            self._critic_optimizer = critic_optimizer or [snt.optimizers.Adam(1e-4) for _ in range(self._agent_number)]
            self._policy_optimizer = policy_optimizer or [snt.optimizers.Adam(1e-4) for _ in range(self._agent_number)]

        # Batch dataset and create iterator.
        self._iterator = dataset_iterator

        # Expose the variables.
        self._variables = {}
        for i in range(self._agent_number):
            policy_network_to_expose = snt.Sequential(
                [self._target_observation_networks[i], self._target_policy_networks[i]])
            self._variables['policy_' + str(i)] = policy_network_to_expose.variables
            self._variables['critic_' + str(i)] = self._target_critic_networks[i].variables
        

        # Create a checkpointer and snapshotter objects.
        self._checkpointer = None
        self._snapshotter = None

        # if checkpoint:
        #     self._checkpointer = tf2_savers.Checkpointer(
        #         subdirectory='DDPG_learner',
        #         objects_to_save={
        #             'counter': self._counter,
        #             'policy': self._policy_network,
        #             'critic': self._critic_network,
        #             'observation': self._observation_network,
        #             'target_policy': self._target_policy_network,
        #             'target_critic': self._target_critic_network,
        #             'target_observation': self._target_observation_network,
        #             'policy_optimizer': self._policy_optimizer,
        #             'critic_optimizer': self._critic_optimizer,
        #             'num_steps': self._num_steps,
        #         })
        #     critic_mean = snt.Sequential(
        #         [self._critic_network, acme_nets.StochasticMeanHead()])
        #     self._snapshotter = tf2_savers.Snapshotter(
        #         objects_to_save={
        #             'policy': self._policy_network,
        #             'critic': critic_mean,
        #         })

        # Do not record timestamps until after the first learning step is done.
        # This is to avoid including the time it takes for actors to come online and
        # fill the replay buffer.
        self._timestamp = None

    @tf.function
    def _step(self, sample) -> Dict[str, tf.Tensor]:
        transitions: types.Transition = sample.data  # Assuming ReverbSample.

        # Cast the additional discount to match the environment discount dtype.
        discount = tf.cast(self._discount, dtype=transitions.discount.dtype)
        
        batch_size = transitions.observation.shape[0]
        
        a_t_list = []
        for i in range(self._agent_number):
            observation = transitions.next_observation[:, i, :]
            o_t = self._target_observation_networks[i](observation)
            o_t = tree.map_structure(tf.stop_gradient, o_t)
            a_t = self._target_policy_networks[i](o_t)
            a_t_list.append(a_t)
        
        agent_next_a_t = tf.concat([a_t_list[i] for i in range(self._agent_number)], axis=1)
        agent_next_a_t = tf.reshape(agent_next_a_t, [batch_size, self._agent_number, self._agent_action_size])
        
        
        critic_losses = []
        policy_losses = []
        for i in range(self._agent_number):
        
            with tf.GradientTape(persistent=True) as tape:
                # Maybe transform the observation before feeding into policy and critic.
                # Transforming the observations this way at the start of the learning
                # step effectively means that the policy and critic share observation
                # network weights.
                o_tm1 = self._observation_networks[i](transitions.observation[:, i, :])
                o_t = self._target_observation_networks[i](transitions.next_observation[:, i, :])
                # This stop_gradient prevents gradients to propagate into the target
                # observation network. In addition, since the online policy network is
                # evaluated at o_t, this also means the policy loss does not influence
                # the observation network training.
                o_t = tree.map_structure(tf.stop_gradient, o_t)

                # Critic learning.
                critic_actions = tf2_utils.batch_concat([
                    transitions.action[:, : i, :],
                    transitions.action[:, i + 1 :, :],
                    transitions.action[:, i, :],
                ]) 
                q_tm1 = self._critic_networks[i](o_tm1, tf.reshape(critic_actions, shape=[batch_size, -1]))
                
                critic_actions = tf2_utils.batch_concat([
                    agent_next_a_t[:, : i, :],
                    agent_next_a_t[:, i + 1 :, :],
                    self._target_policy_networks[i](o_t),
                ])
                q_t = self._target_critic_networks[i](o_t, tf.reshape(critic_actions, shape=[batch_size, -1]))

                # Critic loss.
                critic_loss = losses.categorical(q_tm1, transitions.reward[:, -1],
                                                discount * transitions.discount, q_t)
                critic_loss = tf.reduce_mean(critic_loss, axis=[0])
                critic_losses.append(critic_loss)

                # Actor learning.
                dpg_a_t = self._policy_networks[i](o_t)
                critic_actions = tf2_utils.batch_concat([
                    transitions.action[:, : i, :],
                    transitions.action[:, i + 1 :, :],
                    dpg_a_t,
                ]) 
                
                dpg_z_t = self._critic_networks[i](o_t, critic_actions)
                dpg_q_t = dpg_z_t.mean()

                # Actor loss. If clipping is true use dqda clipping and clip the norm.
                dqda_clipping = 1.0 if self._clipping else None
                policy_loss = losses.dpg(
                    dpg_q_t,
                    critic_actions,
                    tape=tape,
                    dqda_clipping=dqda_clipping,
                    clip_norm=self._clipping)
                policy_loss = tf.reduce_mean(policy_loss, axis=[0])
                policy_losses.append(policy_loss)

            # Get trainable variables.
            policy_variables = self._policy_networks[i].trainable_variables
            critic_variables = (
                # In this agent, the critic loss trains the observation network.
                self._observation_networks[i].trainable_variables +
                self._critic_networks[i].trainable_variables)

            # Compute gradients.
            replica_context = tf.distribute.get_replica_context()
            policy_gradients = _average_gradients_across_replicas(
                replica_context,
                tape.gradient(policy_loss, policy_variables))
            critic_gradients = _average_gradients_across_replicas(
                replica_context,
                tape.gradient(critic_loss, critic_variables))

            # Delete the tape manually because of the persistent=True flag.
            del tape

            # Maybe clip gradients.
            if self._clipping:
                policy_gradients = tf.clip_by_global_norm(policy_gradients, 40.)[0]
                critic_gradients = tf.clip_by_global_norm(critic_gradients, 40.)[0]

            # Apply gradients.
            self._policy_optimizer[i].apply(policy_gradients, policy_variables)
            self._critic_optimizer[i].apply(critic_gradients, critic_variables)

        # Losses to track
        new_critic_losses = tf.reduce_mean(tf.stack(critic_losses, axis=0))
        new_policy_losses = tf.reduce_mean(tf.stack(policy_losses, axis=0))
        
        object_to_return = dict()
        object_to_return['policy_loss'] = new_policy_losses
        object_to_return['critic_loss'] = new_critic_losses
        
        return object_to_return


    @tf.function
    def _replicated_step(self):
        # Update target network
        for i in range(self._agent_number):
            online_variables = (
                *self._observation_networks[i].variables,
                *self._critic_networks[i].variables,
                *self._policy_networks[i].variables,
            )
            target_variables = (
                *self._target_observation_networks[i].variables,
                *self._target_critic_networks[i].variables,
                *self._target_policy_networks[i].variables,
            )

            # Make online -> target network update ops.
            if tf.math.mod(self._num_steps, self._target_update_period) == 0:
                for src, dest in zip(online_variables, target_variables):
                    dest.assign(src)
        self._num_steps.assign_add(1)

        # Get data from replay (dropping extras if any). Note there is no
        # extra data here because we do not insert any into Reverb.
        sample = next(self._iterator)

        # This mirrors the structure of the fetches returned by self._step(),
        # but the Tensors are replaced with replicated Tensors, one per accelerator.
        replicated_fetches = self._replicator.run(self._step, args=(sample,))

        def reduce_mean_over_replicas(replicated_value):
            """Averages a replicated_value across replicas."""
            # The "axis=None" arg means reduce across replicas, not internal axes.
            return self._replicator.reduce(
                reduce_op=tf.distribute.ReduceOp.MEAN,
                value=replicated_value,
            axis=None)

        fetches = tree.map_structure(reduce_mean_over_replicas, replicated_fetches)

        return fetches

    def step(self):
        # Run the learning step.
        fetches = self._replicated_step()

        # Compute elapsed time.
        timestamp = time.time()
        elapsed_time = timestamp - self._timestamp if self._timestamp else 0
        self._timestamp = timestamp

        # Update our counts and record it.
        counts = self._counter.increment(steps=1, walltime=elapsed_time)
        fetches.update(counts)

        # Checkpoint and attempt to write the logs.
        if self._checkpointer is not None:
            self._checkpointer.save()
        if self._snapshotter is not None:
            self._snapshotter.save()
        self._logger.write(fetches)

    def get_variables(self, names: List[str]) -> List[List[np.ndarray]]:
        return [tf2_utils.to_numpy(self._variables[name]) for name in names]


def _get_first_available_accelerator_type(
        wishlist: Sequence[str] = ('TPU', 'GPU', 'CPU')) -> str:
    """Returns the first available accelerator type listed in a wishlist.

    Args:
        wishlist: A sequence of elements from {'CPU', 'GPU', 'TPU'}, listed in
        order of descending preference.

    Returns:
        The first available accelerator type from `wishlist`.

    Raises:
        RuntimeError: Thrown if no accelerators from the `wishlist` are found.
    """
    get_visible_devices = tf.config.get_visible_devices

    for wishlist_device in wishlist:
        devices = get_visible_devices(device_type=wishlist_device)
        if devices:
            return wishlist_device

    available = ', '.join(
        sorted(frozenset([d.type for d in get_visible_devices()])))
    raise RuntimeError(
        'Couldn\'t find any devices from {wishlist}.' +
        f'Only the following types are available: {available}.')


def _average_gradients_across_replicas(replica_context, gradients):
    """Computes the average gradient across replicas.

    This computes the gradient locally on this device, then copies over the
    gradients computed on the other replicas, and takes the average across
    replicas.

    This is faster than copying the gradients from TPU to CPU, and averaging
    them on the CPU (which is what we do for the losses/fetches).

    Args:
        replica_context: the return value of `tf.distribute.get_replica_context()`.
        gradients: The output of tape.gradients(loss, variables)

    Returns:
        A list of (d_loss/d_varabiable)s.
    """

    # We must remove any Nones from gradients before passing them to all_reduce.
    # Nones occur when you call tape.gradient(loss, variables) with some
    # variables that don't affect the loss.
    # See: https://github.com/tensorflow/tensorflow/issues/783
    gradients_without_nones = [g for g in gradients if g is not None]
    original_indices = [i for i, g in enumerate(gradients) if g is not None]

    results_without_nones = replica_context.all_reduce('mean',
                                                        gradients_without_nones)
    results = [None] * len(gradients)
    for ii, result in zip(original_indices, results_without_nones):
        results[ii] = result

    return results