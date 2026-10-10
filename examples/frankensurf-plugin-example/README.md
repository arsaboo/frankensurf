# frankensurf-plugin-example

A provider plugin that reads pages through your own scraping service. Copy it
to start your own; the walkthrough is at
https://frankensurf.dev/docs/more/plugins/.

```bash
pip install -e examples/frankensurf-plugin-example   # into Frankensurf's environment
frankensurf plugins                                   # listed as PLUGIN_NOT_TRUSTED
frankensurf plugins trust provider example_fetch frankensurf-plugin-example
export EXAMPLE_API_URL=https://your-service.example/read EXAMPLE_API_KEY=...
frankensurf read https://example.com --provider example_fetch --paid
```
