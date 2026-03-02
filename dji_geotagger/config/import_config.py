from pathlib import Path
from dji_geotagger.config.default_ppk_dict import DEFAULT_PPK_CONF


def override_rtklib_config(
        user_conf: dict = None, 
        output_path: Path = None) -> Path:
    """
    Merge default RTKLIB configuration with user overrides and export to .conf file.

    Combines DEFAULT_PPK_CONF with optional user-provided settings, then writes
    the merged configuration to a RTKLIB-compatible .conf file that can be used
    with RTKLIB tools (e.g., rnx2rtkp, convbin).

    Parameters
    ----------
    user_conf : dict, optional
        User configuration overrides. Keys should match RTKLIB parameter names.
        Values in this dict will override corresponding entries in DEFAULT_PPK_CONF.
        Default is None (use default config only).
    output_path : Path | str, optional
        Output path for generated .conf file. If not provided, defaults to:
        `./DGT_output/rtklib_config/rtklib_auto.conf`
        Parent directories are created automatically if they don't exist.
        Default is None.

    Returns
    -------
    Path
        Absolute path to the generated RTKLIB configuration file (.conf).
        File format: one parameter per line as "key=value" pairs.

    Raises
    ------
    OSError
        If parent directory cannot be created or file cannot be written
        (permission denied, disk space, etc.).
    TypeError
        If user_conf is not a dict (when provided).

    Notes
    -----
    Default Configuration:
    The default RTKLIB parameters are loaded from DEFAULT_PPK_CONF dictionary.
    User-provided settings override these defaults via dict.update().

    Example
    -------
    >>> user_settings = {
    ...     'pos1-posmode': 'kinematic',
    ...     'pos1-frequency': '1',
    ... }
    >>> conf_path = override_rtklib_config(
    ...     user_conf=user_settings,
    ...     output_path='/path/to/my_config.conf'
    ... )
    >>> print(f"Config saved to: {conf_path}")
    """
    # Handle default output path
    if output_path is None:
        output_path = Path.cwd() / "DGT_output" / "rtklib_config" / "rtklib_auto.conf"
    else:
        output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    config = DEFAULT_PPK_CONF.copy()
    if user_conf:
        config.update(user_conf)

    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        for key, val in config.items():
            f.write(f"{key}={val}\n")

    return output_path