# Selective CI installation

This project declares Rich, Pillow, and rumdl together in
`[project].dependencies`. No dependency groups are needed.

With uvlazy installed, run from this directory:

```sh
uvlazy run rumdl check .
```

Only rumdl and any dependencies it needs are installed. Application packages
remain absent. The included lockfile supplies the tool version.
