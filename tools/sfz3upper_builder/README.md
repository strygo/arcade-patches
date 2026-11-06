# Upper installer maintenance

The CLI checks the presence and hashes of every shipped file. Unrelated files,
including user inputs, tools, notes, and generated output, are allowed and are
not traversed by kit validation. Both manifests must still agree about the
shipped inventory, and required files cannot be symlinks or pass through them.
Release packaging and collection qualification retain their exact inventories.

`--chdman` accepts an executable, a folder containing `chdman.exe` or `chdman`,
or a command available on PATH. Input files and ROM paths must exist. `--work`
and `--out-dir` may be inside the extracted kit; each must be new with an
existing parent. They cannot alias existing data, contain each other, or
overwrite shipped files or source inputs. Failed runs leave their workspace
available for diagnosis; choose a new path for a retry.

Before creating work, the manual CLI checks Python 3.12/3.13 and imports the
pinned NumPy 2.2.6/SciPy 1.15.3 in the same isolated interpreter used for the
reconstruction child. Errors show installation commands for that interpreter
and Windows `py -3.13` guidance. Users can retain newer Python installations.
The builder app continues to prepare its own runtime.

Run the focused suite from the Capcom checkout on configured external storage:

```sh
python3 scripts/agent_storage.py run -- python3 -B -m unittest discover \
  -s ../arcade-patches/tools/sfz3upper_builder -p test_apply.py -v
```

Tests cover the reported input/roms/chdman layout, main's command construction,
in-kit platform export and validation afterward, missing/modified shipped
files, incomplete manifests, path escapes and aliases, executable lookup, and
bounded CLI errors. Reconstruction is stubbed in CLI tests; no game-byte or
hardware qualification is claimed. The generated user readme is maintained in
Capcom's `release/build_sfz3upper_candidate.py`.
