# Community site knowledge

Which tool reads which site, learned by people's own Frankensurf reads and shared as data.

- `routes.json` is what `frankensurf share sync` downloads: route recipes, one per site, each a
  provisional seed (it orders the first attempts for that site; it never grants paid tools or
  identities, and anyone's own recipes and route memory override it).
- `recipes/` holds contributions. Each is the output of `frankensurf share export --out FILE`,
  reviewed by its contributor before the pull request.

## Contributing

```bash
frankensurf share export --out my-recipes.json
```

Read the file. It lists sites (origins) and the tool that read each reliably, with how many reads
back it; paths are cut to their first segment, and nothing read with a profile or identity is
included. Remove anything you'd rather not share, then open a pull request adding it to
`community/recipes/`. A maintainer merges accepted recipes into `routes.json`; a test checks every
file here against the same rules `share sync` applies.
