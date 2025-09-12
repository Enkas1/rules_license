load("@rules_license//rules_gathering:gather_metadata.bzl", "write_metadata_info")

def write_metadata_info_and_fill(ctx, deps, out, version_filler, externals_require_tag = True, ws_marker = None):
    """Writes rules_license metadata JSON then fills missing package_version via git describe."""
    raw = ctx.actions.declare_file(out.basename + ".raw.json")
    write_metadata_info(ctx, deps, raw)

    args = [raw.path, out.path]
    if externals_require_tag:
        args.append("--externals-require-tag")
    inputs = [raw, version_filler]

    if ws_marker:
        args.extend(["--ws-marker", ws_marker.path])
        inputs.append(ws_marker)

    ctx.actions.run(
        inputs = inputs,
        outputs = [out],
        executable = version_filler,
        arguments = args,
        use_default_shell_env = True,
        execution_requirements = {
            "local": "1",
            "no-sandbox": "1",
        },
        progress_message = "rules_license: filling missing package_version via git describe",
    )
