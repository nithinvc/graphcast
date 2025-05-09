# Copyright 2023 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Dataset utilities."""

from pickle import LIST
from re import L
from typing import Any, Mapping, Sequence, Tuple, Union

import dask.array
import numpy as np
import pandas as pd
import xarray

from graphcast import solar_radiation, xarray_tree

TimedeltaLike = Any  # Something convertible to pd.Timedelta.
TimedeltaStr = str  # A string convertible to pd.Timedelta.

TargetLeadTimes = Union[
    TimedeltaLike,
    Sequence[TimedeltaLike],
    slice,  # with TimedeltaLike as its start and stop.
]

_SEC_PER_HOUR = 3600
_HOUR_PER_DAY = 24
SEC_PER_DAY = _SEC_PER_HOUR * _HOUR_PER_DAY
_AVG_DAY_PER_YEAR = 365.24219
AVG_SEC_PER_YEAR = SEC_PER_DAY * _AVG_DAY_PER_YEAR

DAY_PROGRESS = "day_progress"
YEAR_PROGRESS = "year_progress"
_DERIVED_VARS = {
    DAY_PROGRESS,
    f"{DAY_PROGRESS}_sin",
    f"{DAY_PROGRESS}_cos",
    YEAR_PROGRESS,
    f"{YEAR_PROGRESS}_sin",
    f"{YEAR_PROGRESS}_cos",
}
TISR = "toa_incident_solar_radiation"


def get_year_progress(seconds_since_epoch: np.ndarray) -> np.ndarray:
    """Computes year progress for times in seconds.

    Args:
      seconds_since_epoch: Times in seconds since the "epoch" (the point at which
        UNIX time starts).

    Returns:
      Year progress normalized to be in the [0, 1) interval for each time point.
    """

    # Start with the pure integer division, and then float at the very end.
    # We will try to keep as much precision as possible.
    years_since_epoch = (
        seconds_since_epoch / SEC_PER_DAY / np.float64(_AVG_DAY_PER_YEAR)
    )
    # Note depending on how these ops are down, we may end up with a "weak_type"
    # which can cause issues in subtle ways, and hard to track here.
    # In any case, casting to float32 should get rid of the weak type.
    # [0, 1.) Interval.
    return np.mod(years_since_epoch, 1.0).astype(np.float32)


def get_day_progress(
    seconds_since_epoch: np.ndarray,
    longitude: np.ndarray,
) -> np.ndarray:
    """Computes day progress for times in seconds at each longitude.

    Args:
      seconds_since_epoch: 1D array of times in seconds since the 'epoch' (the
        point at which UNIX time starts).
      longitude: 1D array of longitudes at which day progress is computed.

    Returns:
      2D array of day progress values normalized to be in the [0, 1) inverval
        for each time point at each longitude.
    """

    # [0.0, 1.0) Interval.
    day_progress_greenwich = np.mod(seconds_since_epoch, SEC_PER_DAY) / SEC_PER_DAY

    # Offset the day progress to the longitude of each point on Earth.
    longitude_offsets = np.deg2rad(longitude) / (2 * np.pi)
    day_progress = np.mod(
        day_progress_greenwich[..., np.newaxis] + longitude_offsets, 1.0
    )
    return day_progress.astype(np.float32)


def featurize_progress(
    name: str, dims: Sequence[str], progress: np.ndarray
) -> Mapping[str, xarray.Variable]:
    """Derives features used by ML models from the `progress` variable.

    Args:
      name: Base variable name from which features are derived.
      dims: List of the output feature dimensions, e.g. ("day", "lon").
      progress: Progress variable values.

    Returns:
      Dictionary of xarray variables derived from the `progress` values. It
      includes the original `progress` variable along with its sin and cos
      transformations.

    Raises:
      ValueError if the number of feature dimensions is not equal to the number
        of data dimensions.
    """
    if len(dims) != progress.ndim:
        raise ValueError(
            f"Number of feature dimensions ({len(dims)}) must be equal to the"
            f" number of data dimensions: {progress.ndim}."
        )
    progress_phase = progress * (2 * np.pi)
    return {
        name: xarray.Variable(dims, progress),
        name + "_sin": xarray.Variable(dims, np.sin(progress_phase)),
        name + "_cos": xarray.Variable(dims, np.cos(progress_phase)),
    }


def add_derived_vars(data: xarray.Dataset) -> None:
    """Adds year and day progress features to `data` in place if missing.

    Args:
      data: Xarray dataset to which derived features will be added.

    Raises:
      ValueError if `datetime` or `lon` are not in `data` coordinates.
    """

    for coord in ("datetime", "lon"):
        if coord not in data.coords:
            raise ValueError(f"'{coord}' must be in `data` coordinates.")

    # Compute seconds since epoch.
    # Note `data.coords["datetime"].astype("datetime64[s]").astype(np.int64)`
    # does not work as xarrays always cast dates into nanoseconds!
    seconds_since_epoch = (
        data.coords["datetime"].data.astype("datetime64[s]").astype(np.int64)
    )
    batch_dim = ("batch",) if "batch" in data.dims else ()

    # Add year progress features if missing.
    if YEAR_PROGRESS not in data.data_vars:
        year_progress = get_year_progress(seconds_since_epoch)
        data.update(
            featurize_progress(
                name=YEAR_PROGRESS,
                dims=batch_dim + ("time",),
                progress=year_progress,
            )
        )

    # Add day progress features if missing.
    if DAY_PROGRESS not in data.data_vars:
        longitude_coord = data.coords["lon"]
        day_progress = get_day_progress(seconds_since_epoch, longitude_coord.data)
        data.update(
            featurize_progress(
                name=DAY_PROGRESS,
                dims=batch_dim + ("time",) + longitude_coord.dims,
                progress=day_progress,
            )
        )


def add_tisr_var(data: xarray.Dataset) -> None:
    """Adds TISR feature to `data` in place if missing.

    Args:
      data: Xarray dataset to which TISR feature will be added.

    Raises:
      ValueError if `datetime`, 'lat', or `lon` are not in `data` coordinates.
    """

    if TISR in data.data_vars:
        return

    for coord in ("datetime", "lat", "lon"):
        if coord not in data.coords:
            raise ValueError(f"'{coord}' must be in `data` coordinates.")

    # Remove `batch` dimension of size one if present. An error will be raised if
    # the `batch` dimension exists and has size greater than one.
    data_no_batch = data.squeeze("batch") if "batch" in data.dims else data

    tisr = solar_radiation.get_toa_incident_solar_radiation_for_xarray(
        data_no_batch, use_jit=True
    )

    if "batch" in data.dims:
        tisr = tisr.expand_dims("batch", axis=0)

    data.update({TISR: tisr})


def extract_input_target_times(
    dataset: xarray.Dataset,
    input_duration: TimedeltaLike,
    target_lead_times: TargetLeadTimes,
    climate: bool = False,
) -> Tuple[xarray.Dataset, xarray.Dataset]:
    """Extracts inputs and targets for prediction, from a Dataset with a time dim.

    The input period is assumed to be contiguous (specified by a duration), but
    the targets can be a list of arbitrary lead times.

    Examples:

      # Use 18 hours of data as inputs, and two specific lead times as targets:
      # 3 days and 5 days after the final input.
      extract_inputs_targets(
          dataset,
          input_duration='18h',
          target_lead_times=('3d', '5d')
      )

      # Use 1 day of data as input, and all lead times between 6 hours and
      # 24 hours inclusive as targets. Demonstrates a friendlier supported string
      # syntax.
      extract_inputs_targets(
          dataset,
          input_duration='1 day',
          target_lead_times=slice('6 hours', '24 hours')
      )

      # Just use a single target lead time of 3 days:
      extract_inputs_targets(
          dataset,
          input_duration='24h',
          target_lead_times='3d'
      )

    Args:
      dataset: An xarray.Dataset with a 'time' dimension whose coordinates are
        timedeltas. It's assumed that the time coordinates have a fixed offset /
        time resolution, and that the input_duration and target_lead_times are
        multiples of this.
      input_duration: pandas.Timedelta or something convertible to it (e.g. a
        shorthand string like '6h' or '5d12h').
      target_lead_times: Either a single lead time, a slice with start and stop
        (inclusive) lead times, or a sequence of lead times. Lead times should be
        Timedeltas (or something convertible to). They are given relative to the
        final input timestep, and should be positive.

    Returns:
      inputs:
      targets:
        Two datasets with the same shape as the input dataset except that a
        selection has been made from the time axis, and the origin of the
        time coordinate will be shifted to refer to lead times relative to the
        final input timestep. So for inputs the times will end at lead time 0,
        for targets the time coordinates will refer to the lead times requested.
    """
    (target_lead_times, target_duration) = _process_target_lead_times_and_get_duration(
        target_lead_times
    )

    # Shift the coordinates for the time axis so that a timedelta of zero
    # corresponds to the forecast reference time. That is, the final timestep
    # that's available as input to the forecast, with all following timesteps
    # forming the target period which needs to be predicted.
    # This means the time coordinates are now forecast lead times.

    # TODO: This is custom so that the first two frames are always the initial condition

    time = dataset.coords["time"]

    def pp_time(a):
        print(pd.to_timedelta(a.time))

    if climate:
        # NOTE: This is just a heuristic to make the code work.
        # Ideally, we should do some slicing based on the input
        # but for now, we just use the first two steps as input and the third as a target
        cinputs = dataset.isel(time=slice(0, 2))
        ctargets = dataset.isel(time=slice(2, 3))
        # print("input timesteps", pp_time(cinputs))
        # print("output timesteps", pp_time(ctargets))
        return cinputs, ctargets

    dataset = dataset.assign_coords(time=time + target_duration - time[-1])

    # Slice out targets:
    targets = dataset.sel({"time": target_lead_times})

    input_duration = pd.Timedelta(input_duration)
    # Both endpoints are inclusive with label-based slicing, so we offset by a
    # small epsilon to make one of the endpoints non-inclusive:
    zero = pd.Timedelta(0)
    epsilon = pd.Timedelta(1, "ns")
    inputs = dataset.sel({"time": slice(-input_duration + epsilon, zero)})
    return inputs, targets


def _process_target_lead_times_and_get_duration(
    target_lead_times: TargetLeadTimes,
) -> TimedeltaLike:
    """Returns the minimum duration for the target lead times."""
    if isinstance(target_lead_times, slice):
        # A slice of lead times. xarray already accepts timedelta-like values for
        # the begin/end/step of the slice.
        if target_lead_times.start is None:
            # If the start isn't specified, we assume it starts at the next timestep
            # after lead time 0 (lead time 0 is the final input timestep):
            target_lead_times = slice(
                pd.Timedelta(1, "ns"), target_lead_times.stop, target_lead_times.step
            )
        target_duration = pd.Timedelta(target_lead_times.stop)
    else:
        if not isinstance(target_lead_times, (list, tuple, set)):
            # A single lead time, which we wrap as a length-1 array to ensure there
            # still remains a time dimension (here of length 1) for consistency.
            target_lead_times = [target_lead_times]

        # A list of multiple (not necessarily contiguous) lead times:
        target_lead_times = [pd.Timedelta(x) for x in target_lead_times]
        target_lead_times.sort()
        target_duration = target_lead_times[-1]
    return target_lead_times, target_duration


def extract_inputs_targets_forcings(
    dataset: xarray.Dataset,
    *,
    input_variables: Tuple[str, ...],
    target_variables: Tuple[str, ...],
    forcing_variables: Tuple[str, ...],
    pressure_levels: Tuple[int, ...],
    input_duration: TimedeltaLike,
    target_lead_times: TargetLeadTimes,
    climate: bool = False,
) -> Tuple[xarray.Dataset, xarray.Dataset, xarray.Dataset]:
    """Extracts inputs, targets and forcings according to requirements."""
    dataset = dataset.sel(level=list(pressure_levels))

    # "Forcings" include derived variables that do not exist in the original ERA5
    # or HRES datasets, as well as other variables (e.g. tisr) that need to be
    # computed manually for the target lead times. Compute the requested ones.
    if set(forcing_variables) & _DERIVED_VARS:
        add_derived_vars(dataset)
    if set(forcing_variables) & {TISR}:
        add_tisr_var(dataset)

    # `datetime` is needed by add_derived_vars but breaks autoregressive rollouts.
    dataset = dataset.drop_vars("datetime")

    inputs, targets = extract_input_target_times(
        dataset,
        input_duration=input_duration,
        target_lead_times=target_lead_times,
        climate=climate,
    )

    if set(forcing_variables) & set(target_variables):
        raise ValueError(
            f"Forcing variables {forcing_variables} should not "
            f"overlap with target variables {target_variables}."
        )

    inputs = inputs[list(input_variables)]
    # The forcing uses the same time coordinates as the target.
    forcings = targets[list(forcing_variables)]
    targets = targets[list(target_variables)]

    return inputs, targets, forcings


def extend_dataset_in_time(
    dataset: xarray.Dataset,
    required_number_of_steps: int,
    forcing_variables: Tuple[str, ...],
) -> xarray.Dataset:
    """
    Extends a dataset to :int required_number_of_steps.
    """

    supported_forcing_vars = {
        "year_progress_sin",
        "year_progress_cos",
        "day_progress_sin",
        "day_progress_cos",
    }
    assert (
        set(forcing_variables) == supported_forcing_vars
    ), f"Got a forcing variables that are not supported! {set(forcing_variables) - supported_forcing_vars}"

    # Get time deltas - pulled from the rollout
    # Extend the "time" and "datetime" coordinates
    time = dataset.coords["time"]
    # We may not have a dataset which starts at time zero so we have to recenter
    timestep = time[1].data - time[0].data
    if time.shape[0] > 1:
        time_upper = np.asarray(time[1:])
        time_lower = np.asarray(time[:-1])
        assert np.all(timestep == time_upper - time_lower)

    extended_time = np.arange(required_number_of_steps) * timestep

    # It might be the case that we don't start at timestep 0
    start_time_step = time[0].data
    extended_time += start_time_step
    # print("generated new timesteps")

    # Extend the datetime coordinates
    if "datetime" in dataset.coords:
        datetime = dataset.coords["datetime"].data
        first_datetime = datetime[0][0]

        # NOTE: Extended time might have negative time steps? So we need to normalize by the min
        if extended_time[0] != 0:
            normalized_extended_time = (
                extended_time - extended_time[0]
            )  # subtract since negative
        else:
            normalized_extended_time = extended_time
        extended_datetime = normalized_extended_time + first_datetime
        assert np.all(datetime[0] == extended_datetime[: datetime.shape[1]])
        # datetime needs a batch dim
        extended_datetime = np.expand_dims(extended_datetime, 0)
    else:
        extended_datetime = None

    # print("generated new datetime")

    # Helper that extends the time dim
    def extend_time(data_array: xarray.DataArray) -> xarray.DataArray:
        dims = data_array.dims
        if "time" not in dims:
            return data_array  # NOTE: We may have vars that are not dependent on time e.g., land_sea_mask
        shape = list(data_array.shape)
        shape[dims.index("time")] = required_number_of_steps
        dask_data = dask.array.zeros(
            shape=tuple(shape),
            chunks=-1,  # Will give chunk info directly to `ChunksToZarr``.
            dtype=data_array.dtype,
        )

        coords = dict(data_array.coords)
        coords["time"] = extended_time

        if extended_datetime is not None:
            coords["datetime"] = (("batch", "time"), extended_datetime)

        return xarray.DataArray(dims=dims, data=dask_data, coords=coords)

    # _dataset is an in-memory structure of dataset without the data
    _dataset = xarray_tree.map_structure(extend_time, dataset)
    # print("generated a dataset filled with zeros")

    def copy_data(
        empty_array: xarray.DataArray, array: xarray.DataArray
    ) -> xarray.DataArray:
        empty_shape = empty_array.shape
        shape = array.shape

        # Cases:
        # 1. lat, lon
        # 2. batch time lon
        # 3. batch time lat lon
        # 4. batch time level lat lon
        # 5. batch time

        # Case 1 - exact match and copy over
        if empty_shape == shape:
            return array
        # Case 2 - Partial match that needs to be filled in
        elif empty_shape[2:] == array.shape[2:]:
            max_t = array.shape[1]
            empty_array[:, :max_t] = array
            # empty_array[:, max_t:] = np.ones_like(empty_array[:, max_t:]) * np.nan
            return empty_array
        return empty_array

    # Copy the data over
    _dataset = xarray_tree.map_structure(copy_data, _dataset, dataset)
    # print("copied existing data")

    # Fill in the missing forcing functions
    # Drop the existing values of the forcing functions
    for v in supported_forcing_vars:
        if v in _dataset.data_vars:
            _dataset = _dataset.drop_vars(
                {
                    v,
                }
            )
    if "day_progress" in _dataset.keys():
        _dataset = _dataset.drop_vars({"day_progress"})
    if "year_progress" in _dataset.keys():
        _dataset = _dataset.drop_vars({"year_progress"})
    # print("dropped extra vars")
    add_derived_vars(_dataset)
    # print("added derived vars")

    return _dataset


def extract_inputs_targets_forcings_climate(
    dataset: xarray.Dataset,
    *,
    input_variables: Tuple[str, ...],
    target_variables: Tuple[str, ...],
    forcing_variables: Tuple[str, ...],
    pressure_levels: Tuple[int, ...],
    input_duration: TimedeltaLike,
    target_lead_times: TargetLeadTimes,
) -> Tuple[xarray.Dataset, xarray.Dataset, xarray.Dataset]:
    """
    Differs from the normal extract_inputs_targets_forcings in that it allows for a target_lead time
    longer than what is currently available

    This is done by extending the dataset. The extended variables are the forcing variables. Other target vars are filled np.nan.
    """
    # TODO: Hackey since it hard codes the time delta but alas
    target_lead_times, target_duration = _process_target_lead_times_and_get_duration(
        target_lead_times
    )
    # Add two for the input frames
    required_number_steps: int = int(target_duration / pd.Timedelta("12h")) + 2
    # NOTE: Since this is a climate rollout, we always only use 3 steps to rollout
    required_number_steps = 3
    # In fact, this should only be run once by the original dataset. Otherwise,
    # We will always extend the dataset
    if required_number_steps < dataset.time.shape[0]:
        return extract_inputs_targets_forcings(
            dataset,
            input_variables=input_variables,
            target_variables=target_variables,
            forcing_variables=forcing_variables,
            pressure_levels=pressure_levels,
            input_duration=input_duration,
            target_lead_times=target_lead_times,
            climate=True,
        )
    # Need to extend

    dataset = extend_dataset_in_time(dataset, required_number_steps, forcing_variables)
    _inputs, _targets, _forcings = extract_inputs_targets_forcings(
        dataset,
        input_variables=input_variables,
        target_variables=target_variables,
        forcing_variables=forcing_variables,
        pressure_levels=pressure_levels,
        input_duration=input_duration,
        target_lead_times=target_lead_times,
        climate=True,
    )

    assert (
        _inputs.time.shape[0] != 0 and _inputs.time.shape[0] == 2
    ), f"After extension, inputs shape is incorrect. Expected {2} and got {_inputs.time.shape}"

    return _inputs, _targets, _forcings
