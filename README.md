# Serveur MCP des API FHIR du SMT

Serveur [MCP](https://modelcontextprotocol.io/) donnant accès aux Terminologies du [Serveur Multi-Terminologies (SMT)](https://smt.esante.gouv.fr/) de l'Agence du Numérique en Santé, via son API FHIR R4.

Le serveur fonctionne de la même façon pour toutes les Terminologies, jeux de valeurs et alignements accessibles via les API FHIR du SMT.

Il fonctionne en **stdio** : le client MCP (Claude Desktop, Claude Code, VS Code…) le lance localement sur le poste de l'utilisateur.

> [!IMPORTANT]
> Les résultats sont fournis à des fins terminologiques (codage, interopérabilité) et ne remplacent pas une décision clinique. Certaines Terminologies, dont SNOMED CT, sont soumises à des [conditions d'utilisation](./TERMS.md).

Ce serveur s'inspire de celui réalisé par la SNOMED Int. ([snowstorm-mcp-server](https://github.com/IHTSDO/snowstorm-mcp-server)) en élargissant son périmètre à l'ensemble des Terminologies présentes sur le SMT. 

## Sommaire

- [Serveur MCP des API FHIR du SMT](#serveur-mcp-des-api-fhir-du-smt)
  - [Sommaire](#sommaire)
  - [Installation](#installation)
    - [Prérequis](#prérequis)
    - [Paramétrer Claude Code](#paramétrer-claude-code)
    - [Paramétrer Claude Desktop](#paramétrer-claude-desktop)
    - [Paramétrer VS Code](#paramétrer-vs-code)
    - [Mise à jour](#mise-à-jour)
    - [Dépannage](#dépannage)
  - [Utilisation](#utilisation)
  - [Outils](#outils)
  - [Garde-fous](#garde-fous)
  - [Développement](#développement)
  - [Ajouter un outil](#ajouter-un-outil)
    - [1. L'opération FHIR (`fhir.py`)](#1-lopération-fhir-fhirpy)
    - [2. L'implémentation (`runtime.py`)](#2-limplémentation-runtimepy)
    - [3. La déclaration MCP (`mcp_app.py`)](#3-la-déclaration-mcp-mcp_apppy)
    - [4. Les tests](#4-les-tests)
    - [5. La documentation](#5-la-documentation)
  - [Licence](#licence)


## Installation

Il n'y a rien à télécharger à la main ni à configurer : votre outil IA (Claude Code, Claude Desktop ou VS Code) lance lui-même le serveur, qui interroge toujours l'API FHIR publique du SMT (`https://smt.esante.gouv.fr/fhir`).

### Prérequis

1. **uv**, qui installe lui-même une version de Python compatible (≥ 3.11) :

   ```bash
   # macOS / Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   ```powershell
   # Windows (PowerShell)
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   Ouvrez ensuite un nouveau terminal et vérifiez avec `uvx --version`. Autres méthodes : [documentation de uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Un accès Internet à `github.com` (au premier lancement).

Choisissez ensuite votre outil ci-dessous. Tous lancent le serveur avec la même commande :

```bash
uvx --from git+https://github.com/ansforge/interop-outil-serveur-mcp-api-fhir-smt serveur-mcp-api-fhir-smt
```

Au premier lancement, uv télécharge le serveur et ses dépendances, ce qui peut prendre quelques dizaines de secondes. Les lancements suivants utilisent le cache.

### Paramétrer Claude Code

**Option 1 (recommandée) : le plugin**, qui installe le serveur avec les skills `smt-explore` et `snomed-expert`. Dans Claude Code :

```text
/plugin marketplace add ansforge/interop-outil-serveur-mcp-api-fhir-smt
/plugin install serveur-mcp-api-fhir-smt@ans-terminologies
```

**Option 2 : le serveur seul**, dans un terminal :

```bash
claude mcp add --transport stdio --scope user smt \
  -- uvx --from git+https://github.com/ansforge/interop-outil-serveur-mcp-api-fhir-smt serveur-mcp-api-fhir-smt
```

`--scope user` rend le serveur disponible dans tous vos projets. N'utilisez qu'une seule des deux options, sinon le serveur sera déclaré deux fois.

Vérification : `claude mcp list` dans un terminal, ou `/mcp` dans Claude Code, doit afficher `smt` comme connecté.

### Paramétrer Claude Desktop

1. Ouvrez *Paramètres → Développeur → Modifier la configuration*. Cela ouvre le fichier `claude_desktop_config.json` :
   - macOS : `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows : `%APPDATA%\Claude\claude_desktop_config.json`
2. Ajoutez le serveur `smt`. Si le fichier contient déjà un bloc `mcpServers`, ajoutez-y seulement l'entrée `smt` :

   ```json
   {
     "mcpServers": {
       "smt": {
         "command": "uvx",
         "args": ["--from", "git+https://github.com/ansforge/interop-outil-serveur-mcp-api-fhir-smt", "serveur-mcp-api-fhir-smt"]
       }
     }
   }
   ```

3. Quittez complètement Claude Desktop (fermer la fenêtre ne suffit pas), puis relancez-le. Les outils `smt_…` apparaissent alors dans le menu des outils de la conversation.

### Paramétrer VS Code

Les serveurs MCP s'utilisent dans GitHub Copilot Chat, en mode **Agent**.

- Pour un seul projet : créez le fichier `.vscode/mcp.json` à la racine du projet.
- Pour tous vos projets : lancez la commande *MCP: Open User Configuration* (Ctrl+Shift+P / Cmd+Shift+P).

Puis ajoutez :

```json
{
  "servers": {
    "smt": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ansforge/interop-outil-serveur-mcp-api-fhir-smt", "serveur-mcp-api-fhir-smt"]
    }
  }
}
```

Cliquez sur *Start*, qui s'affiche au-dessus de `"smt"` dans le fichier, puis vérifiez que les outils `smt_…` sont cochés dans le sélecteur d'outils du chat.

### Mise à jour

uv réutilise la version en cache. Pour récupérer la dernière version publiée, lancez `uv cache clean serveur-mcp-api-fhir-smt`, puis redémarrez votre outil IA.

### Dépannage

| Symptôme | Solution |
|---|---|
| `spawn uvx ENOENT` ou `uvx` introuvable | Remplacez `"uvx"` dans `command` par son chemin absolu (`which uvx` sous macOS / Linux, `where uvx` sous Windows), puis redémarrez l'outil |
| Le serveur ne démarre pas ou dépasse le délai au premier lancement | Lancez la commande `uvx --from git+https://github.com/ansforge/interop-outil-serveur-mcp-api-fhir-smt serveur-mcp-api-fhir-smt` dans un terminal pour précharger le cache. Le serveur attend alors en silence : quittez avec Ctrl+C, puis redémarrez l'outil |
| Échec du téléchargement depuis GitHub | Derrière un proxy, définissez `HTTPS_PROXY`. Avec un certificat racine d'entreprise, ajoutez aussi `UV_NATIVE_TLS=1` |
| Consulter les journaux | Claude Desktop : `~/Library/Logs/Claude/mcp-server-smt.log` (macOS) ou `%APPDATA%\Claude\logs\mcp-server-smt.log` (Windows). VS Code : *MCP: List Servers → smt → Show Output* |
| Plus de détails dans les journaux | Ajoutez `"--log-level", "DEBUG"` à la fin de `args` pour journaliser chaque requête au SMT |



## Utilisation

Posez vos questions en langage naturel : l'assistant choisit lui-même les [outils](#outils) à appeler. Par exemple :

- « Quel est le code CIM-10 de l'asthme ? »
- « Le code LOINC 2345-7 existe-t-il, et quel est son libellé ? »
- « Liste les enfants directs du code CIM-10 J45. »
- « Quels jeux de valeurs du NOS concernent les spécialités médicales ? »
- « Le concept SNOMED CT 155574008 est inactif : par quoi a-t-il été remplacé ? »



## Outils
Tous les outils sont en lecture seule.

| Outil | Opération FHIR | Usage |
|---|---|---|
| `smt_list_terminologies` | catalogue `CodeSystem` | Trouver l'URL canonique d'une terminologie (« cim 10 », « loinc »…) |
| `smt_list_value_sets` | catalogue `ValueSet` | Trouver un jeu de valeurs (JDV du NOS…) |
| `smt_list_concept_maps` | catalogue `ConceptMap` | Trouver un alignement entre terminologies |
| `smt_lookup` | `CodeSystem/$lookup` | Détail d'un code : libellés, désignations, propriétés, statut |
| `smt_validate_code` | `CodeSystem/$validate-code`, `ValueSet/$validate-code` | Vérifier un code, son libellé, son appartenance à un jeu de valeurs |
| `smt_subsumes` | `CodeSystem/$subsumes` | Relation hiérarchique entre deux codes |
| `smt_expand` | `ValueSet/$expand` | Recherche par libellé dans une terminologie, liste d'un jeu de valeurs, ECL SNOMED CT |
| `smt_get_parents` | `ValueSet/$expand` (filtre `child`) | Parents directs |
| `smt_get_children` | `ValueSet/$expand` (filtre `parent`) | Enfants directs |
| `smt_get_ancestors` | `ValueSet/$expand` (filtre `generalizes`) | Tous les ancêtres |
| `smt_get_descendants` | `ValueSet/$expand` (filtre `descendent-of`) | Toute une sous-hiérarchie |
| `smt_translate` | `ConceptMap/$translate` | Traduire un code vers une autre terminologie |
| `smt_snomed_get_inactivation` | `CodeSystem/$lookup`, `ConceptMap/$translate` (`?fhir_cm=`) | Raison d'inactivation et remplacements d'un concept SNOMED CT inactif |
| `smt_server_health` | `GET /metadata` | État du serveur et opérations prises en charge |



## Garde-fous
Les garde-fous s'appliquent avant et après chaque appel, et renvoient au modèle des messages en français qui expliquent comment reformuler la demande. En stdio, chaque utilisateur lance son propre processus : ces limites s'appliquent **par utilisateur**. Elles protègent le SMT des dérives d'un assistant ; la charge collective du pilote relève des protections du SMT lui-même.

Dans l'ordre où un appel les rencontre :

| Garde-fou | Valeur | Code d'erreur |
|---|---|---|
| Filtrage ECL (`smt_expand` avec `value_set_url`) : refus des motifs SNOMED CT trop coûteux ou non pris en charge par le serveur FHIR. L'URL est déséchappée (HTML, encodage URL) avant contrôle | - | `E_QUERY_BLOCKED` (propose une sous-hiérarchie ou l'outil adapté) |
| Parcours niveau par niveau : enfants des enfants (`smt_get_children`) ou parents des parents (`smt_get_parents`) | 10 codes distincts / minute, par sens | `E_RECURSIVE_TRAVERSAL` (propose `smt_get_descendants` ou `smt_get_ancestors`) |
| Profondeur de pagination : position maximale atteignable en paginant | `offset` < 1 000 | `E_PAGINATION_DEPTH_EXCEEDED` |
| Taille de page : concepts renvoyés par appel | `count` ≤ 100, plafonné sans erreur | - |
| Cache des réponses : un appel identique récent n'interroge pas le SMT | 10 minutes, 1 024 réponses | - |
| Coupe-circuit : suspension sur HTTP 429 et 503 (durée de `Retry-After`) ou après 3 échecs serveur consécutifs. À la fin de chaque pause, un seul appel test ; s'il échoue, la pause est doublée | 30 s, 60 s, 120 s… 5 min au plus | `E_BACKEND_UNAVAILABLE` |
| Blocage définitif : aucune réussite 5 min après le début de la panne (après un dernier appel test). Les appels au SMT restent bloqués jusqu'au redémarrage de l'outil IA de l'utilisateur | 5 min | `E_BACKEND_BLOCKED` |
| Limitation de débit (fenêtre glissante) | 30 requêtes / 60 s | `E_RATE_LIMIT` |
| Plafond de concurrence | 3 requêtes simultanées | (attente) |
| Avertissement sur résultat volumineux | > 1 000 concepts | champ `_large_result_warning` |

Les autres erreurs sont codées de la même façon : `E_BACKEND_HTTP`, `E_BACKEND_TIMEOUT`, `E_BACKEND_AUTH`, `E_BACKEND_REQUEST`, `E_INVALID_ARGUMENT`. Les réponses de plus de 75 000 caractères sont tronquées, avec un avis de troncature.

Ces valeurs, comme l'URL du SMT, le délai des requêtes et la durée de cache du catalogue, sont regroupées dans [config.py](src/serveur_mcp_api_fhir_smt/config.py). Elles ne sont pas modifiables par les utilisateurs : pour en changer une, modifiez ce fichier et publiez une nouvelle version.

## Développement

```bash
uv sync                      # environnement et dépendances
./scripts/check.sh           # lint, formatage, mypy, tests unitaires
uv run pytest -m integration # tests sur l'API réelle du SMT
npx @modelcontextprotocol/inspector uv run serveur-mcp-api-fhir-smt  # test interactif
```

Avec Claude Code, [.mcp.json](.mcp.json) déclare le serveur local `smt` pour ce dépôt. Détails des tests : [docs/testing.md](docs/testing.md). Ajout d'un outil : [section suivante](#ajouter-un-outil).

```
src/serveur_mcp_api_fhir_smt/
├── __main__.py        # point d'entrée (journalisation, stdio)
├── mcp_app.py         # outils MCP, instructions, traduction des erreurs, troncature
├── runtime.py         # état partagé et implémentation des outils
├── fhir.py            # opérations FHIR et modèles de résultat
├── terminology.py     # catalogue CodeSystem / ValueSet / ConceptMap
├── capabilities.py    # sonde du serveur (/metadata)
├── guards.py          # garde-fous
├── http_client.py     # client HTTP, erreurs normalisées
├── config.py          # paramètres du serveur (URL du SMT, délais, cache, garde-fous)
└── logging_utils.py   # journaux sur la sortie d'erreur
tests/                 # tests unitaires, fixtures capturées, tests d'intégration
dev/                   # capture des fixtures sur le SMT
.claude-plugin/        # plugin et marketplace Claude Code
manifest.json          # extension MCPB (Claude Desktop)
```

## Ajouter un outil

Un outil traverse trois couches, chacune dans son module :

| Couche | Module | Rôle |
|---|---|---|
| Opération FHIR | `fhir.py` | Appel au SMT et conversion de la réponse en modèle compact |
| Implémentation | `runtime.py` | Ce que fait l'outil, sans dépendance au SDK MCP |
| Déclaration MCP | `mcp_app.py` | Nom, description pour le modèle, paramètres, garde-fous |

L'exemple ci-dessous ajoute un outil fictif, `smt_get_value_set`, qui décrit un jeu de valeurs (titre, description, terminologies utilisées).

### 1. L'opération FHIR (`fhir.py`)

Définissez un modèle de résultat compact, puis une méthode de `TerminologyService`. Renvoyez seulement ce qui est utile au modèle, pas la ressource FHIR brute.

```python
class ValueSetSummary(BaseModel):
    # Structure #


class TerminologyService:
    ...

    def get_value_set() -> ValueSetSummary:
        # Traitement #
```

Les erreurs HTTP n'ont pas à être interceptées : `http_client.py` les convertit en `HttpRequestError`, que `mcp_app.py` traduit en erreur codée (`E_BACKEND_HTTP`, `E_BACKEND_TIMEOUT`…). N'interceptez une erreur que si elle correspond à une réponse normale, comme `found=false` pour un code inconnu dans la méthode `lookup` de `fhir.py`.

### 2. L'implémentation (`runtime.py`)

```python
    def get_value_set():
        # Traitement #
```

### 3. La déclaration MCP (`mcp_app.py`)

Dans `create_mcp_app`, à côté des outils du même domaine :

```python
@mcp.tool(
    title="Décrire un jeu de valeurs",
    description=("Lorem ipsum"),
    annotations=_READ_ONLY_ANNOTATIONS,
    structured_output=True,
)
def smt_get_value_set():
    # Traitement #
```

Règles à respecter :

- **Tout appel au SMT passe par `_backend(...)`.** Il applique le cache des réponses, le coupe-circuit, la limite de débit, le plafond de concurrence et la traduction des erreurs. Le premier argument identifie l'outil dans la clé de cache ; `args` doit contenir tous les paramètres qui influencent la réponse. Passez `cacheable=False` si la réponse doit toujours refléter l'état du moment, comme pour `smt_server_health`.
- **Description et paramètres en français**, lus par le modèle et affichés aux utilisateurs : ce que fait l'outil, puis « QUAND L'UTILISER » et « PAS POUR », pour l'orienter vers le bon outil. Chaque paramètre a une `Field(description=...)`. Réutilisez les types partagés en tête de module (`System`, `Code`, `Version`, `Offset`, `Count20`…).
- **Outils en lecture seule** : `annotations=_READ_ONLY_ANNOTATIONS` et `structured_output=True`.
- **Garde-fous propres à l'outil**, appelés avant `_backend` :
  - URL de ValueSet implicite pouvant contenir de l'ECL : `guards.check_ecl(value_set_url)` ;
  - pagination : `guards.check_pagination_depth(offset)` et `guards.clamp_page_size(count)`, en mettant la valeur plafonnée dans `args` ;
  - navigation d'un niveau dans une hiérarchie : `guards.check_traversal("down" | "up", system, code)` ;
  - résultat potentiellement volumineux : `guards.add_large_result_warning(...)` dans l'appel, pour l'avertissement.
- **Rien sur la sortie standard** (`print` interdit) : en stdio, elle est réservée au protocole MCP. Utilisez `logging`.
- **Nouveau paramètre réglable** : ajoutez-le à [config.py](src/serveur_mcp_api_fhir_smt/config.py), jamais en option ou variable d'environnement.

### 4. Les tests

1. **Fixture** : ajoutez la requête à `REQUESTS` dans [dev/capture_fixtures.py](dev/capture_fixtures.py), puis lancez `uv run python dev/capture_fixtures.py` pour enregistrer la réponse réelle du SMT dans `tests/fixtures/smt/`.
2. **Opération FHIR** (`tests/test_fhir.py`) : requête envoyée (chemin, paramètres) et analyse de la fixture.
3. **Outil** (`tests/test_mcp_app.py`) : ajoutez le nom à `EXPECTED_TOOLS` et testez un appel complet :

   ```python
   @pytest.mark.anyio
   async def test_get_value_set_tool(app_for):
       # Test #
   ```

4. **Intégration** (`tests/integration/test_smt_live.py`) : mettez à jour le nombre d'outils attendu dans `test_stdio_round_trip`, et ajoutez un test sur le vrai SMT si l'opération est nouvelle.

### 5. La documentation

- le tableau [Outils](#outils) de ce README ;
- la section « CHOIX DE L'OUTIL » de `INSTRUCTIONS` dans `mcp_app.py`, lue par le modèle à la connexion ;

Vérifiez enfin l'ensemble avec `./scripts/check.sh`, puis testez l'outil en conditions réelles avec MCP Inspector ou Claude Code (voir [Développement](#développement)).

## Licence

Ce **serveur MCP** est distribué sous licence MIT.

Le **contenu de la SNOMED CT®** retourné par ce serveur n'est en revanche pas couvert par cette licence. L'accès à la SNOMED CT® via ce serveur est protégé par la licence navigateur de SNOMED CT® ([SNOMED CT Browser License Agreement](https://github.com/IHTSDO/snowstorm-mcp-server/blob/main/SNOMED%20CT%20Browser%20License%20Agreement%20-%202026.pdf)) de la même façon que le [navigateur de la SNOMED International](https://browser.ihtsdotools.org/). En résumé, les utilisateurs finaux, qui ne détiennent pas de Licence d’Affiliation à l’IHTSDO, peuvent accéder à la SNOMED CT® en utilisant ce serveur pour explorer ou évaluer la SNOMED CT®, mais pas pour :
- copier des identifiants de la SNOMED CT® dans un dossier patient, une base de données ou un document (utilisé comme "Système de Création de Données" ou "Système d'Analyse de Données") ;
- traduire ou modifier le contenu de la SNOMED CT® ;
- redistribuer ou partager le contenu de la SNOMED CT®.

Si vous souhaitez héberger ce serveur vous-même, intégrer la SNOMED CT® dans un produit ou un service, ou tout autre utilisation de la SNOMED CT® en dehors de l'exploration et l'évaluation, vous devez obtenir une [licence d'affiliation SNOMED CT®](https://smt.esante.gouv.fr/licences-snomed/). Les affiliés de SNOMED International peuvent utiliser ce serveur selon les conditions de la licence d'affiliation. SNOMED CT est © SNOMED International; "SNOMED" et "SNOMED CT" sont des marques déposées.