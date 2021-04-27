from typing import Tuple, Dict, List, Optional, Callable
from collections import OrderedDict
import itertools

from qpth.qp import QPFunction
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd.functional import jacobian
import pytorch_lightning as pl
from matplotlib.pyplot import figure

from neural_clbf.systems import ControlAffineSystem
from neural_clbf.systems.utils import ScenarioList
from neural_clbf.controllers.utils import Controller
from neural_clbf.experiments.common.episodic_datamodule import EpisodicDataModule


class NeuralCLBFController(pl.LightningModule):
    """
    A neural rCLBF controller
    """

    def __init__(
        self,
        dynamics_model: ControlAffineSystem,
        scenarios: ScenarioList,
        datamodule: EpisodicDataModule,
        clbf_hidden_layers: int = 2,
        clbf_hidden_size: int = 48,
        u_nn_hidden_layers: int = 1,
        u_nn_hidden_size: int = 8,
        clbf_lambda: float = 0.1,
        safety_level: float = 1.0,
        clbf_relaxation_penalty: float = 10.0,
        controller_period: float = 0.01,
        primal_learning_rate: float = 1e-3,
        epochs_per_episode: int = 5,
        plotting_callbacks: Optional[
            List[Callable[[Controller], Tuple[str, figure]]]
        ] = None,
    ):
        """Initialize the controller.

        args:
            dynamics_model: the control-affine dynamics of the underlying system
            scenarios: a list of parameter scenarios to train on
            clbf_hidden_layers: number of hidden layers to use for the CLBF network
            clbf_hidden_size: number of neurons per hidden layer in the CLBF network
            u_nn_hidden_layers: number of hidden layers to use for the proof controller
            u_nn_hidden_size: number of neurons per hidden layer in the proof controller
            clbf_lambda: convergence rate for the CLBF
            safety_level: safety level set value for the CLBF
            clbf_relaxation_penalty: the penalty for relaxing CLBF conditions.
            controller_period: the timestep to use in simulating forward Vdot
            primal_learning_rate: the learning rate for SGD for the network weights
            epochs_per_episode: the number of epochs to include in each episode
            plotting_callbacks: a list of plotting functions that each take a
                                NeuralCLBFController and return a tuple of a string
                                name and figure object to log
        """
        super().__init__()

        # Save the provided model
        self.dynamics_model = dynamics_model
        self.scenarios = scenarios
        self.n_scenarios = len(scenarios)

        # Save the datamodule
        self.datamodule = datamodule

        # Save the other parameters
        self.clbf_lambda = clbf_lambda
        self.safety_level = safety_level
        self.clbf_relaxation_penalty = clbf_relaxation_penalty
        self.controller_period = controller_period
        self.primal_learning_rate = primal_learning_rate
        self.epochs_per_episode = epochs_per_episode

        # Compute and save the center and range of the state variables
        x_max, x_min = dynamics_model.state_limits
        self.x_center = (x_max + x_min) / 2.0
        self.x_range = x_max - x_min

        # Get plotting callbacks
        if plotting_callbacks is None:
            plotting_callbacks = []
        self.plotting_callbacks = plotting_callbacks

        # Define the CLBF network, which we denote V
        self.clbf_hidden_layers = clbf_hidden_layers
        self.clbf_hidden_size = clbf_hidden_size
        # We're going to build the network up layer by layer, starting with the input
        self.V_layers: OrderedDict[str, nn.Module] = OrderedDict()
        self.V_layers["input_layer"] = nn.Linear(
            self.dynamics_model.n_dims, self.clbf_hidden_size
        )
        self.V_layers["input_layer_activation"] = nn.Tanh()
        for i in range(self.clbf_hidden_layers):
            self.V_layers[f"layer_{i}"] = nn.Linear(
                self.clbf_hidden_size, self.clbf_hidden_size
            )
            self.V_layers[f"layer_{i}_activation"] = nn.Tanh()
        self.V_layers["output_layer"] = nn.Linear(self.clbf_hidden_size, 1)
        self.V_nn = nn.Sequential(self.V_layers)

        # Also define the proof controller network, denoted u_nn
        self.u_nn_hidden_layers = u_nn_hidden_layers
        self.u_nn_hidden_size = u_nn_hidden_size
        # Likewise, build the network up layer by layer, starting with the input
        self.u_NN_layers: OrderedDict[str, nn.Module] = OrderedDict()
        self.u_NN_layers["input_layer"] = nn.Linear(
            self.dynamics_model.n_dims, self.u_nn_hidden_size
        )
        self.u_NN_layers["input_layer_activation"] = nn.Tanh()
        for i in range(self.u_nn_hidden_layers):
            self.u_NN_layers[f"layer_{i}"] = nn.Linear(
                self.u_nn_hidden_size, self.u_nn_hidden_size
            )
            self.u_NN_layers[f"layer_{i}_activation"] = nn.Tanh()
        # No output layer, so the control saturates at [-1, 1]
        self.u_NN_layers["output_layer"] = nn.Linear(
            self.u_nn_hidden_size, self.dynamics_model.n_controls
        )
        self.u_NN_layers["output_layer_activation"] = nn.Tanh()
        self.u_NN = nn.Sequential(self.u_NN_layers)

    def normalize(self, x: torch.Tensor) -> torch.Tensor:
        """Normalize the input using the stored center point and range

        args:
            x: bs x self.dynamics_model.n_dims the points to normalize
        """
        return (x - self.x_center.type_as(x)) / self.x_range.type_as(x)

    def V(self, x: torch.Tensor) -> torch.Tensor:
        """Computes the CLBF value from the output layer of V_nn

        args:
            x: bs x self.dynamics_model.n_dims the points at which to evaluate the CLBF
        """
        # Apply the offset and range to normalize about zero
        x = self.normalize(x)

        # Compute the CLBF as the sum-squares of the output layer activations
        V = self.V_nn(x)

        return V

    def u(self, x: torch.Tensor) -> torch.Tensor:
        """Computes the learned controller input from the state x

        args:
            x: bs x self.dynamics_model.n_dims the points at which to evaluate the
               controller
        """
        # Apply the offset and range to normalize about zero
        x = self.normalize(x)

        # Compute the control effort using the neural network
        u = self.u_NN(x)

        # Scale to reflect plant actuator limits
        upper_lim, lower_lim = self.dynamics_model.control_limits
        u_center = (upper_lim + lower_lim).type_as(x) / 2.0
        u_semi_range = (upper_lim - lower_lim).type_as(x) / 2.0

        u_scaled = u * u_semi_range + u_center

        return u_scaled

    def V_lie_derivatives(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """Compute the Lie derivatives of the CLBF V along the control-affine dynamics

        args:
            x: bs x self.dynamics_model.n_dims tensor of state
        returns:
            Lf_V: bs x self.n_scenarios x 1 tensor of Lie derivatives of V
                  along f
            Lg_V: bs x self.n_scenarios x self.dynamics_model.n_controls tensor
                  of Lie derivatives of V along g
        """
        # Get the Jacobian of V for each entry in the batch
        batch_size = x.shape[0]
        J_V_x = torch.zeros(batch_size, 1, x.shape[1])
        J_V_x = J_V_x.type_as(x)
        # Since this might be called in a no_grad environment, we use the
        # enable_grad environment to temporarily accumulate gradients
        with torch.enable_grad():
            for i in range(batch_size):
                J_V_x[i, :, :] = jacobian(
                    self.V, x[i, :].unsqueeze(0), create_graph=True
                )

        # We need to compute Lie derivatives for each scenario
        Lf_V = torch.zeros(batch_size, self.n_scenarios, 1)
        Lg_V = torch.zeros(batch_size, self.n_scenarios, self.dynamics_model.n_controls)
        Lf_V = Lf_V.type_as(x)
        Lg_V = Lg_V.type_as(x)

        for i in range(self.n_scenarios):
            # Get the dynamics f and g for this scenario
            s = self.scenarios[i]
            f, g = self.dynamics_model.control_affine_dynamics(x, params=s)

            # Multiply these with the Jacobian to get the Lie derivatives
            Lf_V[:, i, :] = torch.bmm(J_V_x, f).squeeze(1)
            Lg_V[:, i, :] = torch.bmm(J_V_x, g).squeeze(1)

        # return the Lie derivatives
        return Lf_V, Lg_V

    def forward(self, x):
        """Determine the control input for a given state using a QP

        args:
            x: bs x self.dynamics_model.n_dims tensor of state
        returns:
            u: bs x self.dynamics_model.n_controls tensor of control inputs
        """
        # Get the value of the CLBF and its Lie derivatives
        V = self.V(x)
        Lf_V, Lg_V = self.V_lie_derivatives(x)

        # To find the control input, we want to solve a QP constrained by
        #
        # L_f V + L_g V u + lambda V <= 0
        #
        # To ensure that this QP is always feasible, we relax the constraint
        #
        # L_f V + L_g V u + lambda V - r <= 0
        #                              r >= 0
        #
        # and add the cost term relaxation_penalty * r.
        #
        # The decision variables here are z=[u r], so our quadratic cost is
        # 1/2 z^T Q z + p^T z. We want this cost to equal
        #
        #           ||u - u_nominal||^2 + relaxation_penalty * r^2
        #
        # This reduces to (ignoring constant terms)
        #
        #           u^T I u - 2 u_nominal^T u + relaxation_penalty * r^2
        #
        # so we need
        #
        #           Q = [I 0
        #                0 relaxation_penalty]
        #           p = [-2 u_nominal^T 0]
        #
        # Expressing the constraints formally:
        #
        #       Gz <= h
        #
        # where h = [-L_f V - lambda V, 0]^T and G = [L_g V, -1
        #                                             ... repeated for each scenario
        #                                             0,     -1
        #                                             G_u,    0]
        # We can optionally add the user-specified inequality constraints as G_u
        n_controls = self.dynamics_model.n_controls
        n_scenarios = self.n_scenarios
        bs = x.shape[0]

        # Start by building the cost
        Q = torch.zeros(bs, n_controls + 1, n_controls + 1).type_as(x)
        for j in range(n_controls):
            Q[:, j, j] = 1.0
        Q[:, -1, -1] = self.clbf_relaxation_penalty + 0.01
        p = torch.zeros(bs, n_controls + 1).type_as(x)
        p[:, :-1] = -2.0 * self.dynamics_model.u_nominal(x)

        # Now build the inequality constraints G @ [u r]^T <= h
        G = torch.zeros(
            bs, n_scenarios + 1 + self.G_u.shape[0], n_controls + 1
        ).type_as(x)
        h = torch.zeros(bs, n_scenarios + 1 + self.h_u.shape[0], 1).type_as(x)
        # CLBF decrease condition in each scenario
        for i in range(n_scenarios):
            G[:, i, :n_controls] = Lg_V[:, i, :]
            G[:, i, -1] = -1
            h[:, i, :] = -Lf_V[:, i, :] - self.clbf_lambda * V
        # Positive relaxation
        G[:, n_scenarios, -1] = -1
        h[:, n_scenarios, 0] = 0.0
        # Actuation limits
        G[:, n_scenarios + 1 :, :n_controls] = self.G_u.type_as(x)
        h[:, n_scenarios + 1 :, 0] = self.h_u.view(1, -1).type_as(x)
        h = h.squeeze()
        # No equality constraints
        A = torch.tensor([])
        b = torch.tensor([])

        # Convert to double precision for solving
        Q = Q.double()
        p = p.double()
        G = G.double()
        h = h.double()

        # Solve the QP!
        result: torch.Tensor = QPFunction(verbose=False)(Q, p, G, h, A, b)
        # Extract the results
        u = result[:, :n_controls]

        return u

    def V_loss(
        self,
        x: torch.Tensor,
        goal_mask: torch.Tensor,
        safe_mask: torch.Tensor,
        unsafe_mask: torch.Tensor,
        dist_to_goal: torch.Tensor,
    ) -> List[Tuple[str, torch.Tensor]]:
        """
        Evaluate the loss on the CLBF

        args:
            x: the points at which to evaluate the loss,
            goal_mask: the points in x marked as part of the goal
            safe_mask: the points in x marked safe
            unsafe_mask: the points in x marked unsafe
            dist_to_goal: the distance from x to the goal region
        returns:
            loss: a list of tuples containing ("category_name", loss_value).
        """
        eps = 1e-2
        # Compute loss to encourage satisfaction of the following conditions...
        loss = []
        #   1.) CLBF value should be negative on the goal set.
        V0 = self.V(x[goal_mask])
        goal_term = F.relu(eps + V0)
        loss.append(("CLBF goal term", goal_term.mean()))

        #   2.) 0 <= V <= safety_level in the safe region
        V_safe = self.V(x[safe_mask])
        safe_clbf_term = F.relu(eps + V_safe - self.safety_level) + F.relu(eps - V_safe)
        loss.append(("CLBF safe region term", 100 * safe_clbf_term.mean()))

        # #   2b.) for tuning, V >= dist_to_goal in the safe region
        # safe_tuning_term = F.relu(eps + dist_to_goal[safe_mask] - V_safe)
        # loss.append(("CLBF tuning term", safe_tuning_term.mean()))

        #   3.) V >= safety_level in the unsafe region
        V_unsafe = self.V(x[unsafe_mask])
        unsafe_clbf_term = F.relu(eps + self.safety_level - V_unsafe)
        loss.append(("CLBF unsafe region term", 100 * unsafe_clbf_term.mean()))

        #   4.) A term to encourage satisfaction of CLBF decrease condition
        # Get the current CLBF values
        V = self.V(x)
        # Simulate trajectories forwards using the true dynamics...
        next_num_timesteps = round(self.controller_period / self.dynamics_model.dt)
        x_next = self.simulator_fn(x, next_num_timesteps)[:, -1, :]
        # and get the next CLBF value from the V network
        V_next = self.V(x_next)
        V_change = V_next - self.clbf_lambda * V
        clbf_descent_term = F.relu(eps + V_change).mean()
        loss.append(("CLBF descent term", clbf_descent_term))

        #   4a.) A term to encourage satisfaction of CLBF decrease condition
        # We compute the change in V by simulating x forward in time and checking if V
        # decreases
        clbf_descent_term_sim = torch.tensor(0.0).type_as(x)
        V = self.V(x)
        u_nn = self.u(x)
        for s in self.scenarios:
            next_num_timesteps = round(self.controller_period / self.dynamics_model.dt)
            x_next = self.simulator_fn(x, next_num_timesteps)[:, -1, :]
            V_next = self.V(x_next)
            # dV/dt \approx (V_next - V)/dt + lambda V \leq 0
            # simplifies to V_next - V + dt * lambda V \leq 0
            # simplifies to V_next - (1 + dt * lambda) V \leq 0
            clbf_descent_term_sim += F.relu(
                eps + V_next - (1 - self.clbf_lambda * self.controller_period) * V
            ).mean()
        loss.append(("CLBF descent term (simulated)", clbf_descent_term_sim))

        #   4b.) A term to encourage satisfaction of CLBF decrease condition
        # This time, we compute the decrease using linearization, which provides a
        # training signal for the controller
        clbf_descent_term_lin = torch.tensor(0.0).type_as(x)
        # Get the current value of the CLBF and its Lie derivatives
        # (Lie derivatives are computed using a linear fit of the dynamics)
        # TODO @dawsonc do we need dynamics learning here?
        V = self.V(x)
        Lf_V, Lg_V = self.V_lie_derivatives(x)
        # Get the control and reshape it to bs x n_controls x 1
        u_nn = self.u(x).unsqueeze(-1)
        for i, s in enumerate(self.scenarios):
            # Use these dynamics to compute the derivative of V
            Vdot = Lf_V[:, i, :] + torch.bmm(Lg_V[:, i, :].unsqueeze(1), u_nn)
            clbf_descent_term_lin += F.relu(eps + Vdot + self.clbf_lambda * V).mean()
        loss.append(("CLBF descent term (linearized)", clbf_descent_term_lin))

        return loss

    def u_loss(self, x: torch.Tensor) -> Dict[str, torch.Tensor]:
        """
        Evaluate the loss on the controller

        args:
            x: the points at which to evaluate the loss
        returns:
            loss: a dictionary containing the losses in each category
        """
        loss = {}

        # Currently no control-specific loss terms...
        loss["Controller loss"] = torch.tensor(0.0).type_as(x)

        return loss

    def training_step(self, batch, batch_idx, optimizer_idx):
        """Conduct the training step for the given batch"""
        # Extract the input and masks from the batch
        x, goal_mask, safe_mask, unsafe_mask, dist_to_goal = batch

        # Compute the losses for this optimizer
        component_losses = {}
        self.most_recent_opt_idx = optimizer_idx
        component_losses.update(
            self.V_loss(x, goal_mask, safe_mask, unsafe_mask, dist_to_goal)
        )
        component_losses.update(self.u_loss(x))

        # Compute the overall loss by summing up the individual losses
        total_loss = torch.tensor(0.0).type_as(x)
        # For the objectives, we can just sum them
        for _, loss_value in component_losses.items():
            if not torch.isnan(loss_value):
                total_loss += loss_value

        batch_dict = {"loss": total_loss, **component_losses}

        return batch_dict

    def training_epoch_end(self, outputs):
        """This function is called after every epoch is completed."""
        # Outputs contains a list for each optimizer, and we need to collect the losses
        # from all of them
        outputs = itertools.chain(*outputs)
        # Gather up all of the losses for each component from all batches
        losses = {}
        for batch_output in outputs:
            for key in batch_output.keys():
                # if we've seen this key before, add this component loss to the list
                if key in losses:
                    losses[key].append(batch_output[key])
                else:
                    # otherwise, make a new list
                    losses[key] = [batch_output[key]]

        # Average all the losses
        avg_losses = {}
        for key in losses.keys():
            key_losses = torch.stack(losses[key])
            avg_losses[key] = torch.nansum(key_losses) / key_losses.shape[0]

        # Log the overall loss...
        self.log("Total loss / train", avg_losses["loss"], sync_dist=True)
        # And all component losses
        for loss_key in avg_losses.keys():
            # We already logged overall loss, so skip that here
            if loss_key == "loss":
                continue
            # Log the other losses
            self.log(loss_key + " / train", avg_losses[loss_key], sync_dist=True)

    def validation_step(self, batch, batch_idx):
        """Conduct the validation step for the given batch"""
        # Extract the input and masks from the batch
        x, goal_mask, safe_mask, unsafe_mask, dist_to_goal = batch

        # Get the various losses
        component_losses = {}
        component_losses.update(
            self.V_loss(x, goal_mask, safe_mask, unsafe_mask, dist_to_goal)
        )
        component_losses.update(self.u_loss(x))

        # Compute the overall loss by summing up the individual losses
        total_loss = torch.tensor(0.0).type_as(x)
        # For the objectives, we can just sum them
        for _, loss_value in component_losses.items():
            if not torch.isnan(loss_value):
                total_loss += loss_value

        batch_dict = {"val_loss": total_loss, **component_losses}

        return batch_dict

    def validation_epoch_end(self, outputs):
        """This function is called after every epoch is completed."""
        # Gather up all of the losses for each component from all batches
        losses = {}
        for batch_output in outputs:
            for key in batch_output.keys():
                # if we've seen this key before, add this component loss to the list
                if key in losses:
                    losses[key].append(batch_output[key])
                else:
                    # otherwise, make a new list
                    losses[key] = [batch_output[key]]

        # Average all the losses
        avg_losses = {}
        for key in losses.keys():
            key_losses = torch.stack(losses[key])
            avg_losses[key] = torch.nansum(key_losses) / key_losses.shape[0]

        # Log the overall loss...
        self.log("Total loss / val", avg_losses["val_loss"], sync_dist=True)
        # And all component losses
        for loss_key in avg_losses.keys():
            # We already logged overall loss, so skip that here
            if loss_key == "val_loss":
                continue
            # Log the other losses
            self.log(loss_key + " / val", avg_losses[loss_key], sync_dist=True)

        # **Now entering spicetacular automation zone**
        # We automatically plot and save the CLBF and some simulated rollouts
        # at the end of every validation epoch, using arbitrary plotting callbacks!

        for plot_fn in self.plotting_callbacks:
            plot_name, plot = plot_fn(self)
            self.logger.experiment.add_figure(
                plot_name, plot, global_step=self.current_epoch
            )
        self.logger.experiment.close()
        self.logger.experiment.flush()

    @pl.core.decorators.auto_move_data
    def simulator_fn(self, x_init: torch.Tensor, num_steps: int):
        return self.dynamics_model.simulate(
            x_init,
            num_steps,
            self.forward,
            guard=self.dynamics_model.out_of_bounds_mask,
            controller_period=self.controller_period,
        )

    def on_validation_epoch_end(self):
        """This function is called at the end of every validation epoch"""
        # We want to generate new data at the end of every episode
        if self.current_epoch > 0 and self.current_epoch % self.epochs_per_episode == 0:
            # Use the models simulation function with this controller
            def simulator_fn(x_init: torch.Tensor, num_steps: int):
                return self.dynamics_model.simulate(x_init, num_steps, self.u)

            self.datamodule.add_data(simulator_fn)

    def configure_optimizers(self):
        primal_opt = torch.optim.SGD(
            list(self.V_nn.parameters()) + list(self.u_NN.parameters()),
            lr=self.primal_learning_rate,
            weight_decay=1e-6,
        )

        self.opt_idx_dict = {0: "primal"}

        return [primal_opt]
