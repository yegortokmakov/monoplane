# Monoplane

Monoplane is a tiny tool designed to help you run multiple processes in your local development environment. It is like a very stripped down docker compose for local shell commands. Especially useful for monorepos. Obviously, not for production.

## Using Monoplane

```bash
pip install monoplane
```

Create a `monoplane.yml` file in your project root:

```yaml
color_stderr: "[91m"

services:
  - name: api
    cwd: resources/api
    command: skaffold dev
    build: npm run generate
    color: "[0;33m"
    notes: http://localhost:30005
    env:
      - name: PORT
        value: 30005

  - name: console
    cwd: resources/web_console
    command: npm run dev
    build: npm run build
    color: "[0;36m"
    notes: http://localhost:30001
```

Then run `monoplane` or `mp` in your project root. It will start all services in parallel.

Some useful commands (just press key while running):

- `r` - restart all services
- `s` - print status of all services
- `b` - execute all build commands
- `c` - clear screen

Notes added in the config file will be printed when you press `s`. Useful for adding links to your service endpoints to avoid remembering ports and typing them over and over again.
