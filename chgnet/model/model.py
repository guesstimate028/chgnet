import math
import os

import torch.nn as nn
from torch import Tensor
from chgnet.graph import CrystalGraphConverter, Crystal_Graph
from chgnet.model.layers import *
from chgnet.model.functions import MLP, GatedMLP, find_normalization
from chgnet.model.basis import RadialBessel, Fourier
from chgnet.model.composition_model import Atom_Ref
from pymatgen.core import Structure
from typing import List, Union

datatype = torch.float32


class CHGNet(nn.Module):
    """
    Crystal Hamiltonian Graph neural Network
    A model that takes in a crystal graph and output energy, force, magmom, stress
    """

    def __init__(
        self,
        atom_fea_dim: int = 64,
        bond_fea_dim: int = 64,
        angle_fea_dim: int = 64,
        composition_model: Union[str, nn.Module] = None,
        num_radial: int = 9,
        num_angular: int = 9,
        n_conv: int = 4,
        atom_conv_hidden_dim: Union[List[int], int] = 64,
        update_bond: bool = True,
        bond_conv_hidden_dim: Union[List[int], int] = 64,
        update_angle: bool = False,
        angle_layer_hidden_dim: Union[List[int], int] = 0,
        conv_dropout: float = 0,
        read_out: str = "ave",
        mlp_hidden_dims: Union[List[int], int] = [64, 64],
        mlp_dropout: float = 0,
        mlp_first: bool = False,
        is_intensive: bool = True,
        non_linearity: str = "silu",
        atom_graph_cutoff: int = 5,
        bond_graph_cutoff: int = 3,
        cutofff_coeff: float = 5,
        learnable_rbf: bool = False,
        **kwargs,
    ):
        """
        Define the model here
        Args:
            atom_fea_dim (int): atom feature vector embedding dimension
            bond_fea_dim (int): bond feature vector embedding dimension
            bond_fea_dim (int): angle feature vector embedding dimension
            composition_model (str or nn.Module): attach a composition model to predict energy
                or use str to initialize a pretrained linear regression
            num_radial (int): number of radial basis used in bond basis expansion
            num_angular (int): number of angular basis used in angle basis expansion
            n_conv (int): number of convolution blocks
            atom_conv_hidden_dims (List or int): hidden dimensions of atom convolution layers
            update_bond (bool): whether to use bond_conv_layer to update bond embeddings
            bond_conv_hidden_dim (List or int): hidden dimensions of bond convolution layers
            update_angle (bool): whether to use angle_update_layer to update angle embeddings
            angle_layer_hidden_dim (List or int): hidden dimensions of angle layers
            conv_dropout (float): dropout rate in all conv_layers
            read_out (str): method for pooling layer, 'ave' for standard average pooling,
                'attn' for multi-head attention.
            mlp_hidden_dims (int or list): readout multilayer perceptron hidden dimensions
            mlp_dropout (float): dropout rate in readout MLP
            mlp_first (bool): whether to apply mlp fist then pooling
            atom_graph_cutoff (float): cutoff radius (A) in creating atom_graph,
                this need to be consistent with training dataloader
            bond_graph_cutoff (float): cutoff radius (A) in creating bond_graph,
                this need to be consistent with training dataloader
            cutoff_coeff (float): cutoff strength used in graph smooth cutoff function
            is_intensive: whether the energy training label is intensive
                          i.e. energy/atom
        """
        # Store model args for reconstruction
        self.model_args = {
            k: v
            for k, v in locals().items()
            if k not in ["self", "__class__", "kwargs"]
        }
        self.model_args.update(kwargs)

        super(CHGNet, self).__init__()
        self.atom_fea_dim = atom_fea_dim
        self.bond_fea_dim = bond_fea_dim
        self.is_intensive = is_intensive
        self.n_conv = n_conv

        # Optionally, define composition model
        if type(composition_model) == str:
            self.composition_model = Atom_Ref(is_intensive=is_intensive)
            self.composition_model.initialize_from(composition_model)
        elif isinstance(composition_model, nn.Module):
            self.composition_model = composition_model
        else:
            self.composition_model = None
        if self.composition_model is not None:
            for param in self.composition_model.parameters():
                param.requires_grad = False

        # Define Crystal Graph Converter
        self.graph_converter = CrystalGraphConverter(
            atom_graph_cutoff=atom_graph_cutoff, bond_graph_cutoff=bond_graph_cutoff
        )

        # Define embedding layers
        self.atom_embedding = AtomEmbedding(atom_feature_dim=atom_fea_dim)
        cutoff_coeff = kwargs.pop("cutoff_coeff", 6)
        self.bond_basis_expansion = BondEncoder(
            atom_graph_cutoff=atom_graph_cutoff,
            bond_graph_cutoff=bond_graph_cutoff,
            num_radial=num_radial,
            cutoff_coeff=cutoff_coeff,
            learnable=learnable_rbf,
        )
        self.bond_embedding = nn.Linear(
            in_features=num_radial, out_features=bond_fea_dim, bias=False
        )
        self.bond_weights_ag = nn.Linear(
            in_features=num_radial, out_features=atom_fea_dim, bias=False
        )
        self.bond_weights_bg = nn.Linear(
            in_features=num_radial, out_features=bond_fea_dim, bias=False
        )
        self.angle_basis_expansion = AngleEncoder(
            num_angular=num_angular, learnable=learnable_rbf
        )
        self.angle_embedding = nn.Linear(
            in_features=num_angular, out_features=angle_fea_dim, bias=False
        )

        # Define convolutional layers
        conv_norm = kwargs.pop("conv_norm", None)
        gMLP_norm = kwargs.pop("gMLP_norm", "batch")
        atom_graph_layers = []
        for i in range(n_conv):
            use_mlp_out = False if i == (n_conv - 1) else True
            atom_graph_layers.append(
                AtomConv(
                    atom_fea_dim=atom_fea_dim,
                    bond_fea_dim=bond_fea_dim,
                    hidden_dim=atom_conv_hidden_dim,
                    dropout=conv_dropout,
                    activation=non_linearity,
                    norm=conv_norm,
                    gMLP_norm=gMLP_norm,
                    use_mlp_out=True,
                    resnet=True,
                )
            )
        self.atom_conv_layers = nn.ModuleList(atom_graph_layers)

        if update_bond is True:
            bond_graph_layers = [
                BondConv(
                    atom_fea_dim=atom_fea_dim,
                    bond_fea_dim=bond_fea_dim,
                    angle_fea_dim=angle_fea_dim,
                    hidden_dim=bond_conv_hidden_dim,
                    dropout=conv_dropout,
                    activation=non_linearity,
                    norm=conv_norm,
                    gMLP_norm=gMLP_norm,
                    use_mlp_out=True,
                    resnet=True,
                )
                for _ in range(n_conv - 1)
            ]
            self.bond_conv_layers = nn.ModuleList(bond_graph_layers)
        else:
            self.bond_conv_layers = [None for _ in range(n_conv - 1)]

        if update_angle is True:
            angle_layers = [
                AngleUpdate(
                    atom_fea_dim=atom_fea_dim,
                    bond_fea_dim=bond_fea_dim,
                    angle_fea_dim=angle_fea_dim,
                    hidden_dim=angle_layer_hidden_dim,
                    dropout=conv_dropout,
                    activation=non_linearity,
                    norm=conv_norm,
                    gMLP_norm=gMLP_norm,
                    resnet=True,
                )
                for _ in range(n_conv - 1)
            ]
            self.angle_layers = nn.ModuleList(angle_layers)
        else:
            self.angle_layers = [None for _ in range(n_conv - 1)]

        # Define readout layer
        self.site_wise = nn.Linear(atom_fea_dim, 1)
        self.readout_norm = find_normalization(
            name=kwargs.pop("readout_norm", None), dim=atom_fea_dim
        )
        self.mlp_first = mlp_first
        if mlp_first:
            self.read_out_type = "sum"
            input_dim = atom_fea_dim
            self.pooling = GraphPooling(average=False)
        else:
            if read_out in ["attn", "weighted"]:
                self.read_out_type = "attn"
                num_heads = kwargs.pop("num_heads", 3)
                self.pooling = GraphAttentionReadOut(
                    atom_fea_dim, num_head=num_heads, average=True
                )
                input_dim = atom_fea_dim * num_heads
            else:
                self.read_out_type = "ave"
                input_dim = atom_fea_dim
                self.pooling = GraphPooling(average=True)
        if kwargs.pop("final_mlp", "MLP") in ["normal", "MLP"]:
            self.mlp = MLP(
                input_dim=input_dim,
                hidden_dim=mlp_hidden_dims,
                output_dim=1,
                dropout=mlp_dropout,
                activation=non_linearity,
            )
        else:
            self.mlp = nn.Sequential(
                GatedMLP(
                    input_dim=input_dim,
                    hidden_dim=mlp_hidden_dims,
                    output_dim=mlp_hidden_dims[-1],
                    dropout=mlp_dropout,
                    activation=non_linearity,
                ),
                nn.Linear(in_features=mlp_hidden_dims[-1], out_features=1),
            )

        print(
            "CHGNet initialized with",
            sum(p.numel() for p in self.parameters()),
            f"Parameters, is_intensive={self.is_intensive}",
        )

    def forward(
        self,
        graphs: List[Crystal_Graph],
        task="e",
        return_atom_feas=False,
        return_crystal_feas=False,
    ):
        """
        Get prediction associated with input graphs
        Args:
            graphs (List): a list of Crystal_graphs
            task (str): the prediction task
                        eg: 'e', 'em', 'ef', 'efs', 'efsm'
                        default is 'e'
        Returns:
            model output
        """
        compute_force = "f" in task
        compute_stress = "s" in task
        site_wise = "m" in task

        # Optionally, make composition model prediction
        if self.composition_model is not None:
            comp_energy = self.composition_model(graphs)
        else:
            comp_energy = 0

        # Make batched graph
        batched_graph = BatchedGraph.from_graphs(
            graphs,
            bond_basis_expansion=self.bond_basis_expansion,
            angle_basis_expansion=self.angle_basis_expansion,
            compute_stress=compute_stress,
        )

        # Pass to model
        prediction = self._compute(
            batched_graph,
            site_wise=site_wise,
            compute_force=compute_force,
            compute_stress=compute_stress,
            return_atom_feas=return_atom_feas,
            return_crystal_feas=return_crystal_feas,
        )
        prediction["e"] += comp_energy
        return prediction

    def _compute(
        self,
        g,
        site_wise: bool = False,
        compute_force: bool = False,
        compute_stress: bool = False,
        return_atom_feas: bool = False,
        return_crystal_feas: bool = False,
    ) -> dict:
        """
        Get Energy, Force, Stress, Magmom associated with input graphs
        force = - d(Energy)/d(atom_positions)
        stress = d(Energy)/d(strain)
        Args:
            g (BatchedGraph): batched graph

        Returns:
            prediction (dict): containing the fields:
                e (Tensor) : energy of structures [batch_size, 1]
                f (Tensor) : force on atoms [num_batch_atoms, 3]
                s (Tensor) : stress of structure [3 * batch_size, 3]
                m (Tensor) : magnetic moments of sites [num_batch_atoms, 3]
        """
        prediction = {}
        atoms_per_graph = torch.bincount(g.atom_owners)
        prediction["atoms_per_graph"] = atoms_per_graph

        # Embed Atoms, Bonds and Angles
        atom_feas = self.atom_embedding(
            g.atomic_numbers - 1
        )  # let H be the first embedding column
        bond_feas = self.bond_embedding(g.bond_bases_ag)
        bond_weights_ag = self.bond_weights_ag(g.bond_bases_ag)
        bond_weights_bg = self.bond_weights_bg(g.bond_bases_bg)
        if len(g.angle_bases) != 0:
            angle_feas = self.angle_embedding(g.angle_bases)

        # Message Passing
        for idx, (atom_layer, bond_layer, angle_layer) in enumerate(
            zip(self.atom_conv_layers[:-1], self.bond_conv_layers, self.angle_layers)
        ):

            # Atom Conv
            atom_feas = atom_layer(
                atom_feas=atom_feas,
                bond_feas=bond_feas,
                bond_weights=bond_weights_ag,
                atom_graph=g.batched_atom_graph,
                directed2undirected=g.directed2undirected,
            )

            # Bond Conv
            if len(g.angle_bases) != 0 and bond_layer is not None:
                bond_feas = bond_layer(
                    atom_feas=atom_feas,
                    bond_feas=bond_feas,
                    bond_weights=bond_weights_bg,
                    angle_feas=angle_feas,
                    bond_graph=g.batched_bond_graph,
                )

                # Angle Update
                if angle_layer is not None:
                    angle_feas = angle_layer(
                        atom_feas=atom_feas,
                        bond_feas=bond_feas,
                        angle_feas=angle_feas,
                        bond_graph=g.batched_bond_graph,
                    )
            if idx == self.n_conv - 2:
                if return_atom_feas is True:
                    prediction["atom_fea"] = torch.split(
                        atom_feas, atoms_per_graph.tolist()
                    )
                # Compute site-wise magnetic moments
                if site_wise:
                    magmom = torch.abs(self.site_wise(atom_feas))
                    prediction["m"] = list(
                        torch.split(magmom.view(-1), atoms_per_graph.tolist())
                    )

        # Last conv layer
        atom_feas = self.atom_conv_layers[-1](
            atom_feas=atom_feas,
            bond_feas=bond_feas,
            bond_weights=bond_weights_ag,
            atom_graph=g.batched_atom_graph,
            directed2undirected=g.directed2undirected,
        )
        if self.readout_norm is not None:
            atom_feas = self.readout_norm(atom_feas)

        # Aggregate nodes and ReadOut
        if self.mlp_first:
            energies = self.mlp(atom_feas)
            energy = self.pooling(energies, g.atom_owners).view(-1)
        else:  # ave or attn to create crystal_fea first
            crystal_feas = self.pooling(atom_feas, g.atom_owners)
            energy = self.mlp(crystal_feas).view(-1) * atoms_per_graph
            if return_crystal_feas == True:
                prediction["crystal_fea"] = crystal_feas

        # Compute force
        if compute_force:
            # Need to retain_graph here, because energy is used in loss function,
            # so its gradient need to be calculated later
            # The graphs of force and stress need to be created for same reason.
            force = torch.autograd.grad(
                energy.sum(), g.atom_positions, create_graph=True, retain_graph=True
            )
            force = [-1 * i for i in force]
            prediction["f"] = force

        # Compute stress
        if compute_stress:
            stress = torch.autograd.grad(
                energy.sum(), g.strains, create_graph=True, retain_graph=True
            )
            # Convert Stress unit from eV/A^3 to GPa
            scale = 1 / g.volumes * 160.21766208
            stress = [i * j for i, j in zip(stress, scale)]
            prediction["s"] = stress

        # Normalize energy if model is intensive
        if self.is_intensive:
            energy = energy / atoms_per_graph
        prediction["e"] = energy

        return prediction

    def predict_structure(
        self,
        structure: Union[Structure, List[Structure]],
        task="efsm",
        return_atom_feas=False,
        return_crystal_feas=False,
        batch_size=100,
    ):
        """
        Predict from pymatgen.core.Structure
        Args:
            structure (pymatgen.core.Structure): crystal structure to predict
            task (str): can be 'e' 'ef', 'em', 'efs', 'efsm'
        Returns:
            prediction (dict)
        """
        assert (
            self.graph_converter != None
        ), "self.graph_converter need to be initialized first!"
        if type(structure) == Structure:
            graph = self.graph_converter(structure)
            return self.predict_graph(
                graph,
                task=task,
                return_atom_feas=return_atom_feas,
                return_crystal_feas=return_crystal_feas,
                batch_size=batch_size,
            )
        elif type(structure) == list:
            graphs = [self.graph_converter(i) for i in structure]
            return self.predict_graph(
                graphs,
                task=task,
                return_atom_feas=return_atom_feas,
                return_crystal_feas=return_crystal_feas,
                batch_size=batch_size,
            )
        else:
            raise Exception("input should either be a structure or list of structures!")

    def predict_graph(
        self,
        graph,
        task="e",
        return_atom_feas=False,
        return_crystal_feas=False,
        batch_size=100,
    ):
        if type(graph) == Crystal_Graph:
            self.eval()
            prediction = self.forward(
                [graph],
                task=task,
                return_atom_feas=return_atom_feas,
                return_crystal_feas=return_crystal_feas,
            )
            out = {}
            for key, pred in prediction.items():
                if key == "e":
                    out[key] = pred.item()
                elif key in ["f", "s", "m", "atom_fea"]:
                    assert len(pred) == 1
                    out[key] = pred[0].cpu().detach().numpy()
                elif key == "crystal_fea":
                    out[key] = pred.view(-1).cpu().detach().numpy()
            return out
        elif type(graph) == list:
            self.eval()
            predictions = [{} for _ in range(len(graph))]
            n_steps = math.ceil(len(graph) / batch_size)
            for n in range(n_steps):
                prediction = self.forward(
                    graph[batch_size * n : batch_size * (n + 1)],
                    task=task,
                    return_atom_feas=return_atom_feas,
                    return_crystal_feas=return_crystal_feas,
                )
                for key, pred in prediction.items():
                    if key in ["e"]:
                        for i, e in enumerate(pred.cpu().detach().numpy()):
                            predictions[n * batch_size + i][key] = e
                    elif key in ["f", "s", "m"]:
                        for i, tmp in enumerate(pred):
                            predictions[n * batch_size + i][key] = (
                                tmp.cpu().detach().numpy()
                            )
                    elif key == "atom_fea":
                        for i, atom_fea in enumerate(pred):
                            predictions[n * batch_size + i][key] = (
                                atom_fea.cpu().detach().numpy()
                            )
                    elif key == "crystal_fea":
                        for i, crystal_fea in enumerate(pred.cpu().detach().numpy()):
                            predictions[n * batch_size + i][key] = crystal_fea
            return predictions
        else:
            raise Exception("input should either be a graph or list of graphs!")

    @staticmethod
    def split(x: Tensor, n: Tensor) -> List[Tensor]:
        """
        split a batched result Tensor into a list of Tensors
        """
        print(x, n)
        start = 0
        result = []
        for i in n:
            result.append(x[start : start + i])
            start += i
        assert start == len(x), "Error: source tensor not correctly split!"
        return result

    def as_dict(self):
        out = {"state_dict": self.state_dict(), "model_args": self.model_args}
        return out

    @classmethod
    def from_dict(cls, dict, **kwargs):
        """
        build a Crystal Hamiltonian Graph Network from a saved dictionary
        """
        chgnet = CHGNet(**dict["model_args"])
        chgnet.load_state_dict(dict["state_dict"], **kwargs)
        return chgnet

    @classmethod
    def from_file(cls, path, **kwargs):
        """
        build a Crystal Hamiltonian Graph Network from a path
        """
        state = torch.load(path, map_location=torch.device("cpu"))
        chgnet = CHGNet.from_dict(state["model"], **kwargs)
        return chgnet

    @classmethod
    def load(cls, model_name="MPtrj-efsm"):
        """
        build a Crystal Hamiltonian Graph Network from a saved dictionary
        """
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if model_name == "MPtrj-efsm":
            return cls.from_file(
                os.path.join(current_dir, "../pretrained/e30f75s350m33.pth.tar")
            )
        else:
            raise Exception("model_name not supported")


class AtomEmbedding(nn.Module):
    """
    Encode an atom by its atomic number using a learnable embedding layer
    """

    def __init__(self, atom_feature_dim: int, max_num_elements: int = 94):
        """
        Initialize the Atom featurizer
        Args:
            atom_feature_dim (int): dimension of atomic embedding
        """
        super().__init__()
        self.embedding = nn.Embedding(max_num_elements, atom_feature_dim)

    def forward(self, atomic_numbers: Tensor) -> Tensor:
        """
        Convert the structure to a atom embedding tensor
        Args:
            atomic_numbers (Tensor): [n_atom, 1]
        Returns:
            atom_fea (Tensor): atom embeddings [n_atom, atom_feature_dim]
        """
        return self.embedding(atomic_numbers)


class BondEncoder(nn.Module):
    """
    Encode a chemical bond given the position of two atoms using Gaussian Distance.
    """

    def __init__(
        self,
        atom_graph_cutoff: float = 5,
        bond_graph_cutoff: float = 3,
        num_radial: int = 5,
        cutoff_coeff: int = 5,
        learnable: bool = False,
    ):
        """
        Initialize the bond encoder
        Args:
            bond_feature_dim (int): dimension of bond embedding
        """
        super().__init__()
        self.rbf_expansion_ag = RadialBessel(
            num_radial=num_radial,
            cutoff=atom_graph_cutoff,
            smooth_cutoff=cutoff_coeff,
            learnable=learnable,
        )
        self.rbf_expansion_bg = RadialBessel(
            num_radial=num_radial,
            cutoff=bond_graph_cutoff,
            smooth_cutoff=cutoff_coeff,
            learnable=learnable,
        )

    def forward(
        self,
        center: Tensor,
        neighbor: Tensor,
        undirected2directed: Tensor,
        image: Tensor,
        lattice: Tensor,
    ) -> (Tensor, Tensor, Tensor):
        """
        Compute the pairwise distance between 2 3d coordinates
        Args:
            center (Tensor): 3d cartesian coordinates of center atoms [n_bond, 3]
            neighbor (Tensor): 3d cartesian coordinates of neighbor atoms [n_bond, 3]
            image (Tensor): the periodic image specifying the location of neighboring atom [n_bond, 3]
            lattice (Tensor): the lattice of this structure [3, 3]
        Returns:
            bond_weights (Tensor): weights determined by distance between pos1 and pos2 [n_bond]
            bond_vectors (Tensor): normalized bond vectors [n_bond, 3]
            bond_features (Tensor): bond feature tensor [n_bond, bond_feature_dim]
        """
        neighbor = neighbor + image @ lattice
        bond_vectors = center - neighbor
        bond_lengths = torch.norm(bond_vectors, dim=1)
        # Normalize the bond vectors
        bond_vectors = bond_vectors / bond_lengths[:, None]

        # We create bond features only for undirected bonds
        # atom1 -> atom2 and atom2 -> atom1 should share same bond_basis
        undirected_bond_lengths = torch.index_select(
            bond_lengths, 0, undirected2directed
        )
        bond_basis_ag = self.rbf_expansion_ag(undirected_bond_lengths)
        bond_basis_bg = self.rbf_expansion_bg(undirected_bond_lengths)
        return bond_basis_ag, bond_basis_bg, bond_vectors


class AngleEncoder(nn.Module):
    """
    Encode an angle given the two bond vectors using Fourier Expansion.
    """

    def __init__(self, num_angular: int = 21, learnable: bool = True):
        """
        Initialize the angle encoder
        Args:
            num_angular (int): number of angular basis to use
            (Note: num_angular can only be an odd number)
        """
        super().__init__()
        assert (num_angular - 1) % 2 == 0, "angle_feature_dim can only be odd integer!"
        circular_harmonics_order = int((num_angular - 1) / 2)
        self.fourier_expansion = Fourier(
            order=circular_harmonics_order, learnable=learnable
        )

    def forward(self, bond_i: Tensor, bond_j: Tensor) -> Tensor:
        """
        Compute the angles between normalized vectors
        Args:
            bond_i (Tensor): normalized left bond vector [n_angle, 3]
            bond_j (Tensor): normalized right bond vector [n_angle, 3]
        Returns:
            angle_fea (Tensor):  expanded cos_ij [n_angle, angle_feature_dim]
        """
        cosine_ij = torch.sum(bond_i * bond_j, dim=1) * (
            1 - 1e-6
        )  # for torch.acos stability
        angle = torch.acos(cosine_ij)
        result = self.fourier_expansion(angle)
        return result


class BatchedGraph(object):
    """
    Batched crystal graph for parallel computing
    """

    def __init__(
        self,
        atomic_numbers: Tensor,
        bond_bases_ag: Tensor,
        bond_bases_bg: Tensor,
        angle_bases: Tensor,
        batched_atom_graph: Tensor,
        batched_bond_graph: Tensor,
        atom_owners: Tensor,
        directed2undirected: Tensor,
        atom_positions: List[Tensor],
        strains: List[Tensor],
        volumes: [Tensor],
    ):
        """
        Batched crystal graph
        Args:
            atomic_numbers (Tensor): atomic numbers vector [num_batch_atoms]
            bond_bases_ag (Tensor): bond bases vector for atom_graph
                [num_batch_bonds, num_radial]
            bond_bases_bg (Tensor): bond bases vector for atom_graph
                [num_batch_bonds, num_radial]
            angle_bases (Tensor): angle bases vector [num_batch_angles, num_angular]
            batched_atom_graph (Tensor) : batched atom graph adjacency list
                [num_batch_bonds, 2]
            batched_bond_graph (Tensor) : bond graph adjacency list
                [num_batch_angles, 2]
            atom_owners (Tensor): graph indices for each atom, used aggregate batched
                graph back to single graph
                [num_batch_atoms]
            directed2undirected (Tensor): the utility tensor used to quickly
                map directed edges to undirected edges in graph
                [num_directed]
            atom_positions (List[Tensor]): cartesian coordinates of the atoms from structures
                [num_batch_atoms]
            strains (List[Tensor]): a list of strains that's initialized to be zeros
                [batch_size]
            volumes (Tensor): the volume of each structure in the batch
                [batch_size]
        """
        self.atomic_numbers = atomic_numbers
        self.bond_bases_ag = bond_bases_ag
        self.bond_bases_bg = bond_bases_bg
        self.angle_bases = angle_bases
        self.batched_atom_graph = batched_atom_graph
        self.batched_bond_graph = batched_bond_graph
        self.atom_owners = atom_owners
        self.directed2undirected = directed2undirected
        self.atom_positions = atom_positions
        self.strains = strains
        self.volumes = volumes

    @classmethod
    def from_graphs(
        cls,
        graphs: List[Crystal_Graph],
        bond_basis_expansion: nn.Module,
        angle_basis_expansion: nn.Module,
        compute_stress: bool = False,
    ):
        """
        Featurize and assemble a list of graphs
        Args:
            graphs (List[Tensor]): a list of Crystal_Graphs
            bond_basis_expansion (nn.Module): bond basis expansion layer in CHGNet
            angle_basis_expansion (nn.Module): angle basis expansion layer in CHGNet
            compute_stress (bool): whether to compute stress
        Returns:
            assembled batch_graph that contains all information for model
        """
        atomic_numbers, atom_positions = [], []
        lattice_feas, strains, volumes = [], [], []
        bond_bases_ag, bond_bases_bg, angle_bases = [], [], []
        batched_atom_graph, batched_bond_graph = [], []
        directed2undirected = []
        atom_owners = []
        atom_offset_idx = 0
        n_undirected = 0

        for graph_idx, graph in enumerate(graphs):
            # Atoms
            n_atom = graph.atomic_number.shape[0]
            atomic_numbers.append(graph.atomic_number)

            # Lattice
            if compute_stress:
                strain = graph.lattice.new_zeros([3, 3], requires_grad=True)
                lattice = graph.lattice @ (torch.eye(3).to(strain.device) + strain)
            else:
                strain = None
                lattice = graph.lattice
            volumes.append(torch.det(lattice))
            strains.append(strain)

            # Bonds
            atom_cart_coords = graph.atom_frac_coord @ lattice
            bond_basis_ag, bond_basis_bg, bond_vectors = bond_basis_expansion(
                center=atom_cart_coords[graph.atom_graph[:, 0]],
                neighbor=atom_cart_coords[graph.atom_graph[:, 1]],
                undirected2directed=graph.undirected2directed,
                image=graph.neighbor_image,
                lattice=lattice,
            )
            atom_positions.append(atom_cart_coords)
            bond_bases_ag.append(bond_basis_ag)
            bond_bases_bg.append(bond_basis_bg)

            # Indexes
            batched_atom_graph.append(graph.atom_graph + atom_offset_idx)
            directed2undirected.append(graph.directed2undirected + n_undirected)

            # Angles
            if len(graph.bond_graph) != 0:
                bond_vecs_i = torch.index_select(
                    bond_vectors, 0, graph.bond_graph[:, 2]
                )
                bond_vecs_j = torch.index_select(
                    bond_vectors, 0, graph.bond_graph[:, 4]
                )
                angle_basis = angle_basis_expansion(bond_vecs_i, bond_vecs_j)
                angle_bases.append(angle_basis)

                bond_graph = graph.bond_graph.new_zeros([graph.bond_graph.shape[0], 3])
                bond_graph[:, 0] = graph.bond_graph[:, 0] + atom_offset_idx
                bond_graph[:, 1] = graph.bond_graph[:, 1] + n_undirected
                bond_graph[:, 2] = graph.bond_graph[:, 3] + n_undirected
                batched_bond_graph.append(bond_graph)

            atom_owners.append(torch.ones(n_atom, requires_grad=False) * graph_idx)
            atom_offset_idx += n_atom
            n_undirected += len(bond_basis_ag)

        # Make Torch Tensors
        atomic_numbers = torch.cat(atomic_numbers, dim=0)
        bond_bases_ag = torch.cat(bond_bases_ag, dim=0)
        bond_bases_bg = torch.cat(bond_bases_bg, dim=0)
        if len(angle_bases) != 0:
            angle_bases = torch.cat(angle_bases, dim=0)
        else:
            angle_bases = torch.tensor([])
        batched_atom_graph = torch.cat(batched_atom_graph, dim=0)
        if batched_bond_graph != []:
            batched_bond_graph = torch.cat(batched_bond_graph, dim=0)
        else:  # when bond graph is empty or disabled
            batched_bond_graph = torch.tensor([])
        atom_owners = (
            torch.cat(atom_owners, dim=0).type(torch.int).to(atomic_numbers.device)
        )
        directed2undirected = torch.cat(directed2undirected, dim=0)
        volumes = torch.tensor(volumes, dtype=datatype, device=atomic_numbers.device)

        return BatchedGraph(
            atomic_numbers=atomic_numbers,
            bond_bases_ag=bond_bases_ag,
            bond_bases_bg=bond_bases_bg,
            angle_bases=angle_bases,
            batched_atom_graph=batched_atom_graph,
            batched_bond_graph=batched_bond_graph,
            atom_owners=atom_owners,
            directed2undirected=directed2undirected,
            atom_positions=atom_positions,
            strains=strains,
            volumes=volumes,
        )