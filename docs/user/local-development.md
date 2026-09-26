# Local development

```shell
pdm run python scripts/bootstrap_env.py
pdm run dev
```

The startup wrapper selects an available host port and prints the final URL. The `web` log reports container port 8000; use the printed host URL.

Create the first administrator:

```shell
docker compose exec web /app/.venv/bin/python manage.py create_instance_admin USERNAME
```

Run local checks with `pdm run check`. Run real PostgreSQL proof with:

```shell
pdm run python scripts/run_isolated_test_lane.py postgres
```
