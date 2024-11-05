"""Module for describing, applying and reverting Galilean transformations on simulations.

Generally it is not necessary to access this module directly, but it is used by
:class:`~pynbody.snapshot.simsnap.SimSnap` class to implement e.g.
:meth:`~pynbody.snapshot.simsnap.SimSnap.rotate_x` etc, and also by analysis
transformations such as :func:`~pynbody.analysis.halo.center` and
:func:`~pynbody.analysis.angmom.faceon` etc.

The main feature of this module is the :class:`Transformation` class, which performs
transformations of various kinds on simulations then reverts them.  You can use these objects
as context managers, which means you can write code like this:

>>> with f.translate(fshift) :
>>>     print(f['pos'][0])

which is equivalent to

>>> try:
>>>     f['pos']+=shift
>>>     print(f['pos'][0])
>>> finally:
>>>     f['pos']-=shift

You can also chain transformations together, like this:

>>> with f.translate(fshift).rotate_x(45):
>>>    print(f['pos'][0])

This implies a translation followed by a rotation. When reverting the transformation, they are
of course undone in the opposite order.

.. versionchanged:: 2.0

   The transformation system has been changed in version 2.0. While transformations are active they are now
   applied to newly loaded data. In particular, if a simulation is rotated and then a 3d array is loaded,
   the array will be rotated to match the simulation. This is a change from previous versions where transformations
   were only applied to arrays instantaneously in RAM, which could lead to inconsistent behaviour.

   As a consequence of this change, the system is also more strict about the order in which transformations are reverted.
   One cannot revert a transformation when another transformation has been applied after it (and not yet reverted).
"""

from __future__ import annotations

import typing
import weakref

if typing.TYPE_CHECKING:
    from . import snapshot
    from typing import Optional

import numpy as np

from . import util


class TransformationException(Exception):
    """Exception raised when a transformation fails"""

    pass

class Transformable:
    """A mixin class for objects that can generate a Transformation object"""
    def __init__(self):
        self._current_transformation = None

    def current_transformation(self) -> Transformation | None:
        """Return the transformation that has been applied to this object, if any."""
        return self._current_transformation

    def _register_transformation(self, t: Transformation):
        if self._current_transformation is not None:
            self._current_transformation._register_transformation(t)
        else:
            self._current_transformation = t

    def _deregister_transformation(self, t: Transformation):
        if self._current_transformation is t:
            if self.current_transformation().current_transformation() is not None:
                raise TransformationException("This transformation cannot be reverted as it is not the most recent transformation")
            else:
                self._current_transformation = None
        elif self._current_transformation is not None:
            self._current_transformation._deregister_transformation(t)
        else:
            raise TransformationException("This transformation cannot be reverted as it cannot be found in the transformation chain")

    def translate(self, offset):
        """Translate by the given offset.

        Returns a :class:`pynbody.transformation.GenericTranslation` object which can be used
        as a context manager to ensure that the translation is undone.

        For more information, see the :mod:`pynbody.transformation` documentation."""
        return GenericTranslation(self, 'pos', offset, description="translate")

    def offset_velocity(self, offset):
        """Shift the velocity by the given offset.

        Returns a :class:`pynbody.transformation.GenericTranslation` object which can be used
        as a context manager to ensure that the velocity shift is undone.

        For more information, see the :mod:`pynbody.transformation` documentation."""
        return GenericTranslation(self, 'vel', offset, description = "offset_velocity")

    def rotate_x(self, angle):
        """Rotates about the current x-axis by 'angle' degrees.

        Returns a :class:`pynbody.transformation.Rotation` object which can be used
        as a context manager to ensure that the rotation is undone.

        For more information, see the :mod:`pynbody.transformation` documentation."""
        angle_rad = angle * np.pi / 180
        return self.rotate(np.array([[1, 0, 0],
                                     [0, np.cos(angle_rad), -np.sin(angle_rad)],
                                     [0, np.sin(angle_rad),  np.cos(angle_rad)]]),
                           description = f"rotate_x({angle})")

    def rotate_y(self, angle):
        """Rotates about the current y-axis by 'angle' degrees.

        Returns a :class:`pynbody.transformation.Rotation` object which can be used
        as a context manager to ensure that the rotation is undone.

        For more information, see the :mod:`pynbody.transformation` documentation."""
        angle_rad = angle * np.pi / 180
        return self.rotate(np.array([[np.cos(angle_rad), 0, np.sin(angle_rad)],
                                     [0,                1,        0],
                                     [-np.sin(angle_rad),   0,   np.cos(angle_rad)]]),
                           description = f"rotate_y({angle})")

    def rotate_z(self, angle):
        """Rotates about the current z-axis by 'angle' degrees.

        Returns a :class:`pynbody.transformation.Rotation` object which can be used
        as a context manager to ensure that the rotation is undone.

        For more information, see the :mod:`pynbody.transformation` documentation."""
        angle_rad = angle * np.pi / 180
        return self.rotate(np.array([[np.cos(angle_rad), -np.sin(angle_rad), 0],
                                     [np.sin(angle_rad),  np.cos(angle_rad), 0],
                                     [0,             0,        1]]),
                           description = f"rotate_z({angle})")

    def rotate(self, matrix, description = None):
        """Rotates using a specified matrix.

        Returns a :class:`pynbody.transformation.Rotation` object which can be used
        as a context manager to ensure that the translation is undone.

        For more information, see the :mod:`pynbody.transformation` documentation.

        Parameters
        ----------

        matrix : array_like
            The 3x3 orthogonal matrix to rotate by

        description : str
            A description of the rotation to be returned from str() and repr()
        """
        return Rotation(self, matrix, description = description)

    @util.deprecated("This method is deprecated and will be removed in a future version. Use the rotate method instead.")
    def transform(self, matrix):
        """Deprecated alias for :meth:`rotate`."""
        return self.rotate(matrix)


class Transformation(Transformable):
    """The base class for all transformations.

    Note that this class inherits from :class:`Transformable`, so all transformations are themselves
    transformable. This means that you can chain transformations together, e.g. for a
    :class:`~pynbody.snapshot.simsnap.SimSnap` object *f*:

    >>> with f.translate(fshift).rotate_x(45):
    >>>    ...
    """

    def __init__(self, f, description = None):
        """Initialise a transformation, and apply it if not explicitly deferred

        Parameters
        ----------
        f : Transformable
            The simulation or transformation to act on. If a transformation is given, this
            transformation will be chained to it, i.e. the result will represent the composed
            transformation.

        description : str
            A description of the transformation to be returned from str() and repr()
        """
        from . import snapshot

        super().__init__()

        if isinstance(f, NullTransformation):
            f = f.sim # as though we are starting from the simulation itself

        if isinstance(f, snapshot.SimSnap):
            self.sim = f
            self._previous_transformation = None
        elif isinstance(f, Transformation):
            self.sim = f.sim
            self._previous_transformation = f
        else:
            raise TypeError("Transformation must either act on another Transformation or on a SimSnap")

        self._description = description
        self._entered = False

        self._apply(self.sim)
        # not apply_to as we don't want to chain -- any underlying transformations will be applied already

        self.sim._register_transformation(self)

    @property
    def sim(self) -> snapshot.SimSnap | None:
        """The simulation to which this transformation applies"""
        return self._sim()

    @sim.setter
    def sim(self, sim):
        if sim is None:
            self._sim = lambda: None
        else:
            self._sim = weakref.ref(sim)

    def __repr__(self):
        return "<Transformation " + str(self) + ">"

    def __str__(self):
        if self._previous_transformation is not None:
            s = str(self._previous_transformation) + ", "
        else:
            s = ""

        return s + self._describe()

    def _describe(self):
        if self._description:
            return self._description
        else:
            return self.__class__.__name__

    def apply_to(self, f: snapshot.SimSnap) -> snapshot.SimSnap:
        """Apply this transformation to a specified simulation.

        Chained transformations are applied recursively.

        Parameters
        ----------
        f : SimSnap
            The simulation to apply the transformation to. Any simulation reference stored within
            the transformation itself is ignored

        Returns
        -------
        SimSnap
            The input simulation (not a copy)
        """
        if self._previous_transformation is not None:
            # this is a chained transformation, get the SimSnap to operate on
            # from the level below
            f = self._previous_transformation.apply_to(f)

        self._apply(f)

        return f

    def apply_inverse_to(self, f):
        """Apply the inverse of this transformation to a specified simulation.

        Chained transformations are applied recursively.

        Parameters
        ----------
        f : SimSnap
            The simulation to apply the transformation to

        Returns
        -------
        SimSnap
            The input simulation (not a copy)
        """
        self._revert(f)

        if self._previous_transformation is not None:
            self._previous_transformation.apply_inverse_to(f)


    def revert(self):
        """Revert the transformation. If it has not been applied, a RuntimeError is raised."""

        self.sim._deregister_transformation(self)
        self._revert(self.sim)

        if self._previous_transformation is not None:
            self._previous_transformation.revert()

    def _apply(self, f):
        pass

    def _revert(self, f):
        pass

    def __enter__(self):
        if self._entered:
            raise TransformationException("Transformation cannot be reapplied")
        self._entered = True
        return self

    def __exit__(self, *args):
        self.revert()


class NullTransformation(Transformation):
    """A transformation that does nothing, provided for convenience where a transformation is expected."""

    def __init__(self, f):
        super().__init__(f, description="null")

    def _apply(self, f):
        pass
    def _revert(self, f):
        pass


class GenericTranslation(Transformation):
    """A translation on a specified array of a simulation"""

    def __init__(self, f, arname, shift, description = None):
        """Initialise a translation on a named array

        Parameters
        ----------
        f : Transformable
            The transformable to act on
        arname : str
            The name of the array to translate
        shift : array_like
            The shift to apply
        description : str
            A description of the translation to be returned from str() and repr()
        """
        self.shift = shift
        self.arname = arname
        super().__init__(f, description=description)

    def _apply(self, f):
        f[self.arname] += self.shift

    def _revert(self, f):
        f[self.arname] -= self.shift


class Rotation(Transformation):
    """A rotation on all 3d vectors in a simulation, by a given orthogonal 3x3 matrix"""

    def __init__(self, f, matrix, ortho_tol=1.e-8, description = None):
        """Initialise a rotation on a simulation.

        The matrix must be orthogonal to within *ortho_tol*.

        Parameters
        ----------
        f : SimSnap
            The simulation to act on

        matrix : array_like
            The 3x3 orthogonal matrix to rotate by

        ortho_tol : float
            The tolerance for orthogonality of the matrix. If the matrix is not orthogonal to within
            this tolerance, a ValueError is raised.

        description: str
            A description of the rotation to be returned from str() and repr()


        """
        # Check that the matrix is orthogonal
        resid = np.dot(matrix, np.asarray(matrix).T) - np.eye(3)
        resid = (resid ** 2).sum()
        if resid > ortho_tol or resid != resid:
            raise ValueError("Transformation matrix is not orthogonal")
        self.matrix = matrix
        if description is None:
            description = "rotate"
        super().__init__(f, description=description)

    def _apply(self, f):
        self._transform(self.matrix)

    def _revert(self, f):
        self._transform(self.matrix.T)

    def _transform(self, matrix):
        """Transforms the snapshot according to the 3x3 matrix given."""

        sim = self.sim

        # NB though it might seem more efficient to access _arrays and
        # _family_arrays directly, this would not work for SubSnaps.
        snapshot_keys = sim.keys()

        for array_name in snapshot_keys:
            ar = sim[array_name]
            if (not ar.derived) and len(ar.shape) == 2 and ar.shape[1] == 3:
                ar[:] = np.dot(matrix, ar.transpose()).transpose()

        for fam in sim.families():
            family_keys = sim[fam].keys()
            family_keys_not_in_snapshot = set(family_keys) - set(snapshot_keys)
            for array_name in family_keys_not_in_snapshot:
                ar = sim[fam][array_name]
                if (not ar.derived) and len(ar.shape) == 2 and ar.shape[1] == 3:
                    ar[:] = np.dot(matrix, ar.transpose()).transpose()


GenericRotation = Rotation # name from pynbody v1


@util.deprecated("This function is deprecated and will be removed in a future version. Use the translate method of a SimSnap object instead.")
def translate(f, shift):
    """Deprecated alias for ``f.translate(shift)``"""

    return GenericTranslation(f, 'pos', shift)

@util.deprecated("This function is deprecated and will be removed in a future version. Use the translate method of a SimSnap object instead.")
def inverse_translate(f, shift):
    """Deprecated alias for ``f.translate(-shift)``"""
    return translate(f, -np.asarray(shift))

@util.deprecated("This function is deprecated and will be removed in a future version. Use the offset_velocity method of a SimSnap object instead.")
def v_translate(f, shift):
    """Deprecated alias for ``f.offset_velocity(shift)``"""

    return GenericTranslation(f, 'vel', shift)

@util.deprecated("This function is deprecated and will be removed in a future version. Use the offset_velocity method of a SimSnap object instead.")
def inverse_v_translate(f, shift):
    """Deprecated alias for ``f.offset_velocity(-shift)``"""

    return GenericTranslation(f, 'vel', -np.asarray(shift))

@util.deprecated("This function is deprecated and will be removed in a future version. Use sim.translate(...).vel_translate(...) instead.")
def xv_translate(f, x_shift, v_shift):
    """Deprecated alias for ``f.translate(x_shift).offset_velocity(v_shift)``"""

    return translate(v_translate(f, v_shift), x_shift)

@util.deprecated("This function is deprecated and will be removed in a future version. "
                 "Use sim.translate(...).vel_translate(...) instead.")
def inverse_xv_translate(f, x_shift, v_shift):
    """Deprecated alias for ``f.translate(-x_shift).offset_velocity(-v_shift)``"""

    return translate(v_translate(f, -np.asarray(v_shift)),
                     -np.asarray(x_shift))

@util.deprecated("This function is deprecated and will be removed in a future version. "
                 "Use the rotate method of a SimSnap object instead.")
def transform(f, matrix):
    """Deprecated alias for ``f.rotate(matrix)``"""

    return Rotation(f, matrix)


@util.deprecated("This function is deprecated and will be removed in a future version. Use NullTransformation instead.")
def null(f):
    """Deprecated alias for ``NullTransformation(f)``"""

    return NullTransformation(f)
