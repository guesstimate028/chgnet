import sys
from typing import Optional, Union
import pickle
import contextlib

import numpy as np
import torch
from chgnet.model import CHGNet
from ase import Atoms, units
from ase.calculators.calculator import Calculator, all_changes, all_properties
from ase.constraints import ExpCellFilter
from ase.io import Trajectory
from ase.md.nptberendsen import NPTBerendsen, Inhomogeneous_NPTBerendsen
from ase.md.nvtberendsen import NVTBerendsen
from ase.optimize.bfgs import BFGS
from ase.optimize.bfgslinesearch import BFGSLineSearch
from ase.optimize.fire import FIRE
from ase.optimize.lbfgs import LBFGS, LBFGSLineSearch
from ase.optimize.mdmin import MDMin
from ase.optimize.optimize import Optimizer
from ase.optimize.sciopt import SciPyFminBFGS, SciPyFminCG
from pymatgen.core.structure import Molecule, Structure
from pymatgen.io.ase import AseAtomsAdaptor

OPTIMIZERS = {
    "FIRE": FIRE,
    "BFGS": BFGS,
    "LBFGS": LBFGS,
    "LBFGSLineSearch": LBFGSLineSearch,
    "MDMin": MDMin,
    "SciPyFminCG": SciPyFminCG,
    "SciPyFminBFGS": SciPyFminBFGS,
    "BFGSLineSearch": BFGSLineSearch,
}


class CHGNetCalculator(Calculator):
    """
    CHGNet Calculator for ASE applications
    """

    implemented_properties = ["energy", "forces", "stress", "magmoms"]

    def __init__(
        self,
        model: CHGNet = None,
        use_device: str = None,
        stress_weight: float = 1 / 160.21766208,
        **kwargs,
    ):
        """
        Args:
            model (CHGNet): instance of a chgnet model
            use_device (str, optional): The device to be used for predictions,
                either "cpu", "cuda", or "mps".
                If not specified, the default device is automatically
                selected based on the available options.
        """
        # Call super class constructor
        super().__init__(**kwargs)

        # Determine the device to use
        if use_device != None:
            self.device = use_device
        elif torch.cuda.is_available():
            self.device = "cuda"
        #  mps is disabled
        # elif torch.backends.mps.is_available():
        #     self.device = 'mps'
        else:
            self.device = "cpu"

        # Move the model to the specified device
        if model is None:
            model = CHGNet.load()
        self.model = model.to(self.device)
        self.stress_weight = stress_weight

    def calculate(
        self,
        atoms: Optional[Atoms] = None,
        desired_properties: Optional[list] = None,
        changed_properties: Optional[list] = None,
    ):
        """
        Calculate various properties of the atoms using CHGNet.

        Parameters:
        - atoms (Optional[Atoms]): The atoms object to calculate properties for.
        - desired_properties (Optional[list]): The properties to calculate. Default is all properties.
        - changed_properties (Optional[list]): The changes made to the system. Default is all changes.

        Returns:
        None
        """
        desired_properties = desired_properties or all_properties
        changed_properties = changed_properties or all_changes
        super().calculate(
            atoms=atoms,
            properties=desired_properties,
            system_changes=changed_properties,
        )

        # Run CHGNet
        structure = AseAtomsAdaptor.get_structure(atoms)
        graph = self.model.graph_converter(structure)
        model_prediction = self.model.predict_graph(graph.to(self.device), task="efsm")

        # Convert Result
        factor = 1 if not self.model.is_intensive else structure.composition.num_atoms
        self.results.update(
            energy=model_prediction["e"] * factor,
            forces=model_prediction["f"],
            free_energy=model_prediction["e"] * factor,
            magmoms=model_prediction["m"],
            stress=model_prediction["s"] * self.stress_weight,
        )


class StructOptimizer:
    """
    Wrapper class for structural relaxation
    """

    def __init__(
        self,
        model: CHGNet = None,
        optimizer_class: Optional[Union[Optimizer, str]] = "FIRE",
        stress_weight: float = 1 / 160.21766208,
        use_device: str = None,
    ):
        if isinstance(optimizer_class, str):
            if optimizer_class in OPTIMIZERS:
                optimizer_class = OPTIMIZERS[optimizer_class]
            else:
                raise ValueError(
                    f"Optimizer instance not found. Select one from {str(list(OPTIMIZERS.keys()))}"
                )

        self.optimizer_class = optimizer_class
        self.calculator = CHGNetCalculator(
            model=model, stress_weight=stress_weight, use_device=use_device
        )

    def relax(
        self,
        atoms: Union[Structure, Atoms],
        fmax: Optional[float] = 0.1,
        steps: Optional[int] = 500,
        relax_cell: Optional[bool] = True,
        save_path: Optional[str] = None,
        trajectory_save_interval: Optional[int] = 1,
        **kwargs,
    ):
        if isinstance(atoms, Structure):
            atoms = AseAtomsAdaptor.get_atoms(atoms)

        atoms.set_calculator(self.calculator)

        stream = sys.stdout
        with contextlib.redirect_stdout(stream):
            obs = TrajectoryObserver(atoms)
            if relax_cell:
                atoms = ExpCellFilter(atoms)
            optimizer = self.optimizer_class(atoms, **kwargs)
            optimizer.attach(obs, interval=trajectory_save_interval)
            optimizer.run(fmax=fmax, steps=steps)
            obs()
        if save_path is not None:
            obs.save(save_path)

        if isinstance(atoms, ExpCellFilter):
            atoms = atoms.atoms
        struc = AseAtomsAdaptor.get_structure(atoms)
        for k in struc.site_properties.keys():
            struc.remove_site_property(property_name=k)
        struc.add_site_property(
            "magmom", [float(i) for i in atoms.get_magnetic_moments()]
        )
        return {
            "final_structure": struc,
            "trajectory": obs,
        }


class TrajectoryObserver:
    """
    Trajectory observer is a hook in the relaxation process that saves the
    intermediate structures
    """

    def __init__(self, atoms: Atoms):
        """
        Args:
            atoms (Atoms): the structure to observe
        """
        self.atoms = atoms
        self.energies: list[float] = []
        self.forces: list[np.ndarray] = []
        self.stresses: list[np.ndarray] = []
        self.magmoms: list[np.ndarray] = []
        self.atom_positions: list[np.ndarray] = []
        self.cells: list[np.ndarray] = []

    def __call__(self):
        """
        The logic for saving the properties of an Atoms during the relaxation
        Returns:
        """
        self.energies.append(self.compute_energy())
        self.forces.append(self.atoms.get_forces())
        self.stresses.append(self.atoms.get_stress())
        self.magmoms.append(self.atoms.get_magnetic_moments())
        self.atom_positions.append(self.atoms.get_positions())
        self.cells.append(self.atoms.get_cell()[:])

    def compute_energy(self) -> float:
        """
        calculate the energy, here we just use the potential energy
        Returns:
        """
        energy = self.atoms.get_potential_energy()
        return energy

    def save(self, filename: str):
        """
        Save the trajectory to file
        Args:
            filename (str): filename to save the trajectory
        Returns:
        """
        with open(filename, "wb") as f:
            pickle.dump(
                {
                    "energy": self.energies,
                    "forces": self.forces,
                    "stresses": self.stresses,
                    "magmoms": self.magmoms,
                    "atom_positions": self.atom_positions,
                    "cell": self.cells,
                    "atomic_number": self.atoms.get_atomic_numbers(),
                },
                f,
            )


class MolecularDynamics:
    """
    Molecular dynamics class
    """

    def __init__(
        self,
        atoms: Union[Atoms, Structure],
        model: CHGNet = None,
        ensemble: str = "nvt",
        temperature: int = 300,
        timestep: float = 1.0,
        pressure: float = 1.01325 * units.bar,
        taut: Optional[float] = None,
        taup: Optional[float] = None,
        compressibility_au: Optional[float] = None,
        trajectory: Optional[Union[str, Trajectory]] = None,
        logfile: Optional[str] = None,
        loginterval: int = 1,
        append_trajectory: bool = False,
        use_device: str = "cpu",
    ):
        """
        Args:
            atoms (Atoms): atoms to run the MD
            model (CHGNet): model
            ensemble (str): choose from 'nvt' or 'npt'. NPT is not tested,
                use with extra caution
            temperature (float): temperature for MD simulation, in K
            timestep (float): time step in fs
            pressure (float): pressure in eV/A^3
            taut (float): time constant for Berendsen temperature coupling
            taup (float): time constant for pressure coupling
            compressibility_au (float): compressibility of the material in A^3/eV
            trajectory (str or Trajectory): Attach trajectory object
            logfile (str): open this file for recording MD outputs
            loginterval (int): write to log file every interval steps
            append_trajectory (bool): Whether to append to prev trajectory
        """
        if isinstance(atoms, (Structure, Molecule)):
            atoms = AseAtomsAdaptor.get_atoms(atoms)

        self.atoms = atoms
        self.atoms.set_calculator(CHGNetCalculator(model, use_device=use_device))

        if taut is None:
            taut = 100 * timestep * units.fs
        if taup is None:
            taup = 1000 * timestep * units.fs

        if ensemble.lower() == "nvt":
            self.dyn = NVTBerendsen(
                self.atoms,
                timestep * units.fs,
                temperature_K=temperature,
                taut=taut,
                trajectory=trajectory,
                logfile=logfile,
                loginterval=loginterval,
                append_trajectory=append_trajectory,
            )

        elif ensemble.lower() == "npt":
            """
            NPT ensemble default to Inhomogeneous_NPTBerendsen thermo/barostat
            This is a more flexible scheme that fixes three angles of the unit
            cell but allows three lattice parameter to change independently.
            """

            self.dyn = Inhomogeneous_NPTBerendsen(
                self.atoms,
                timestep * units.fs,
                temperature_K=temperature,
                pressure_au=pressure,
                taut=taut,
                taup=taup,
                compressibility_au=compressibility_au,
                trajectory=trajectory,
                logfile=logfile,
                loginterval=loginterval,
            )

        elif ensemble.lower() == "npt_berendsen":
            """
            This is a similar scheme to the Inhomogeneous_NPTBerendsen.
            This is a less flexible scheme that fixes the shape of the
            cell - three angles are fixed and the ratios between the three
            lattice constants.
            """

            self.dyn = NPTBerendsen(
                self.atoms,
                timestep * units.fs,
                temperature_K=temperature,
                pressure_au=pressure,
                taut=taut,
                taup=taup,
                compressibility_au=compressibility_au,
                trajectory=trajectory,
                logfile=logfile,
                loginterval=loginterval,
                append_trajectory=append_trajectory,
            )

        else:
            raise ValueError("Ensemble not supported")

        self.trajectory = trajectory
        self.logfile = logfile
        self.loginterval = loginterval
        self.timestep = timestep

    def run(self, steps: int):
        """
        Thin wrapper of ase MD run
        Args:
            steps (int): number of MD steps
        Returns:
        """
        self.dyn.run(steps)

    def set_atoms(self, atoms: Atoms):
        """
        Set new atoms to run MD
        Args:
            atoms (Atoms): new atoms for running MD
        Returns:
        """
        calculator = self.atoms.calc
        self.atoms = atoms
        self.dyn.atoms = atoms
        self.dyn.atoms.set_calculator(calculator)