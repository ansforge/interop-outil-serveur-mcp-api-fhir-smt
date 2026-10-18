# Tests

## Tests unitaires (rapides, sans réseau)

```bash
uv sync
uv run pytest
```

Les appels HTTP sont simulés avec `httpx.MockTransport`. Les réponses du serveur viennent de **fixtures capturées sur le SMT réel** (`tests/fixtures/smt/`), ce qui garantit que l'analyse des réponses correspond au comportement effectif d'Ontoserver.

Lint, formatage, typage et tests unitaires en une commande (identique à la CI) :

```bash
./scripts/check.sh
```

## Tests d'intégration (API réelle du SMT)

Marqués `@pytest.mark.integration`, ils sont exclus par défaut :

```bash
uv run pytest -m integration
```

Ils couvrent le catalogue, la consultation, la recherche textuelle, la hiérarchie, ECL SNOMED CT, ainsi qu'un échange complet en stdio (`initialize`, `tools/list`, `tools/call`) avec le serveur lancé en sous-processus.

En CI, ils sont lancés chaque semaine et à la demande (*Actions → CI → Run workflow*), sans bloquer les pull requests.

## Mettre à jour les fixtures

Quand le SMT change de version :

```bash
uv run python dev/capture_fixtures.py
uv run pytest
git diff tests/fixtures/
```

Examinez le diff : une modification de structure des réponses doit être reportée dans `fhir.py` avant de valider les nouvelles fixtures.

## Ajouter un outil

La procédure complète, avec un exemple et les tests à écrire, est décrite dans le [README](../README.md#ajouter-un-outil).
