# Zimmplayer — API

[![Docker](https://github.com/ririmi56/zimmplayer-back/actions/workflows/docker.yml/badge.svg)](https://github.com/ririmi56/zimmplayer-back/actions/workflows/docker.yml)

Backend d'un lecteur de musique auto-hébergé pour une bibliothèque stockée sur
S3/MinIO, conçu pour fonctionner sur un **réseau airgap** : aucune dépendance
réseau externe à l'exécution. Le client web correspondant vit dans le dépôt
sœur [`zimmplayer-front`](https://github.com/ririmi56/zimmplayer-front) ;
l'orchestration Docker Compose et le livrable airgap, dans
[`zimmplayer-deploy`](https://github.com/ririmi56/zimmplayer-deploy).

Le bucket est organisé en `Artiste/Album/NN - Titre.ext`. Les métadonnées
embarquées dans les fichiers sont indexées une fois en base, ce qui permet de
servir le catalogue sans jamais relister le bucket ; l'audio, lui, est streamé
à la demande.

## Architecture

```
Navigateur ──── reverse proxy ────┬── /api/*  → FastAPI (ce dépôt)
                                   └── /s3/*   → MinIO

  Lecture d'un titre :
    1. GET /api/tracks/42/stream
    2. ← 302 vers une URL présignée (TTL 1 h)
    3. GET direct sur /s3/… avec en-tête Range → 206 Partial Content
```

Trois décisions structurent le projet :

**Le flux audio ne transite pas par l'API.** Elle renvoie une redirection vers
une URL présignée ; le navigateur télécharge directement depuis MinIO et gère
lui-même le déplacement dans le morceau. L'API ne consomme donc ni bande
passante ni worker pendant la lecture.

**MinIO est exposé sous `/s3` par le même reverse proxy que le front.** Tout
est en même-origine (aucun CORS à configurer), MinIO n'est jamais publié
directement. Attention : en SigV4, le chemin *et* le Host sont couverts par la
signature — le proxy retire le préfixe `/s3` et l'API signe sans lui (voir
`app/services/s3.public_stream_url`).

**Les corrections manuelles ne touchent jamais le bucket.** Chaque champ
éditable existe en deux exemplaires : `source_<champ>` (ce que disent les tags)
et `<champ>` (ce qui est affiché, trié et recherché). Une correction est stockée
à part et réappliquée après chaque scan ; la retirer restaure la valeur du
fichier.

## Image Docker

Publiée sur GHCR à chaque push sur `master` (voir
[`.github/workflows/docker.yml`](./.github/workflows/docker.yml)), en
`linux/amd64` et `linux/arm64` :

```bash
docker pull ghcr.io/ririmi56/zimmplayer-back:latest
```

| Tag | Correspond à |
|---|---|
| `latest` | dernier commit sur `master` |
| `X.Y.Z`, `X.Y` | tag Git `vX.Y.Z` |
| `<sha court>` | un commit précis, pour figer ou revenir en arrière |

Les migrations Alembic s'appliquent automatiquement au démarrage du conteneur
(`docker-entrypoint.sh`) — rien à lancer à la main lors d'une mise à jour.

Le conteneur écoute sur `:8000` et attend une base MariaDB et un MinIO déjà
joignables. Variables d'environnement principales (voir
[`.env.example`](./.env.example) pour la liste complète, y compris Snapcast) :

| Variable | Rôle |
|---|---|
| `DATABASE_URL` | Connexion MariaDB, ex. `mysql+pymysql://user:pass@host:3306/db` |
| `S3_ENDPOINT` | MinIO vu par l'API (interne) |
| `S3_PUBLIC_BASE_URL` | MinIO vu par le **navigateur** (URLs présignées) — voir la section Architecture |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET` | Identifiants et bucket S3/MinIO |

## Développement

Nécessite une base MariaDB et un MinIO déjà démarrés.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env      # ajuster selon votre environnement
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

Après toute modification de l'API, régénérer les types du front (dans
`zimmplayer-front`) :

```bash
npx openapi-typescript http://localhost:8000/api/openapi.json -o src/api/schema.d.ts
```

## Tests

```bash
.venv/bin/pytest
```

Couvre les tags, les corrections manuelles, et les fonctions pures de
l'intégration Snapcast (le reste — flux TCP, ffmpeg, snapserver — se vérifie
de bout en bout contre un vrai snapserver).

## Le scan

Trois phases, dont une seule télécharge quoi que ce soit.

1. **Listing** — `list_objects_v2` paginé renvoie clé, ETag, taille et date sans
   lire le contenu. Les extensions hors `AUDIO_EXTENSIONS` sont écartées.
2. **Lecture** — un objet est téléchargé **si et seulement si** son couple
   `(ETag, taille)` diffère de celui enregistré, ou s'il est absent de la base.
   L'ETag étant dérivé du contenu, toute modification du fichier déclenche la
   relecture ; un ré-upload à l'identique ne déclenche rien. `last_modified`
   n'entre pas dans la comparaison.
3. **Suppression** — les pistes dont l'objet a disparu sortent du catalogue,
   puis les albums et artistes vides sont purgés.

| Situation | LIST | GetObject |
|---|---|---|
| Premier scan, N fichiers | ⌈N/1000⌉ | N |
| Rescan sans changement | ⌈N/1000⌉ | **0** |
| M fichiers modifiés | ⌈N/1000⌉ | M |

**Scan complet** (`POST /api/admin/scan?force=true`) : ignore la comparaison et
relit tout. À réserver aux évolutions de `app/services/tags.py`, puisqu'un scan
normal ne relira jamais un fichier inchangé.

À noter : quand un album n'a aucune pochette, `covers.find_folder_cover` tente
5 clés (`cover.jpg`, `folder.jpg`…) pour **chaque** piste de cet album. Ce sont
des 404, donc pas de transfert, mais ce n'est pas gratuit.

## Sessions d'écoute et Snapcast

### Le point à comprendre en premier

**Un navigateur ne peut pas alimenter Snapcast, mais il peut le consommer.**
Snapserver ne lit pas une URL : il consomme un flux PCM brut poussé dans une de
ses sources — le *produire* est donc le travail du serveur. En revanche, rien
n'empêche le navigateur d'être un **snapclient** : snapserver expose le même
protocole binaire sur WebSocket (voir `zimmplayer-front/src/snapcast/`).

En mode Snapcast, le front est donc les deux à la fois :

- **télécommande** — il pilote la file et la lecture par l'API REST ;
- **snapclient** — il joue le flux, synchronisé avec les enceintes, et apparaît
  dans les groupes de snapserver comme n'importe quel appareil.

```
MinIO ──► ffmpeg -re ──► PCM s16le 48k ──► TCP ──► snapserver ──┬─► enceintes
                                                                │
                    navigateur ◄── WebSocket (PCM) ── API ◄─────┘
                        └──────── commandes REST ──► API
```

C'est ce qui rend l'identification automatique : le navigateur *est* un client
identifié, il n'y a rien à déclarer.

### Un seul port vers snapserver

L'API parle à snapserver par **deux canaux, sur le même port** — celui de son
serveur HTTP intégré (`SNAPCAST_HTTP_PORT`, 1780 par défaut) :

| Canal | Chemin | Qui s'en sert |
|---|---|---|
| Contrôle JSON-RPC 2.0 | `/jsonrpc` | `services/snapcast.call()` — volumes, groupes, flux |
| Audio (protocole binaire) | `/stream` | relayé au navigateur par `/api/snapcast/stream` |

Le **port de contrôle TCP 1705 n'est plus utilisé**. Il porte le même JSON-RPC,
mais le serveur HTTP est le seul port exposé dans la plupart des déploiements,
et il permet de tout faire passer par une WebSocket — donc par un reverse proxy
TLS, ce qu'un socket TCP brut ne permet pas aussi simplement. Les deux canaux
sont construits par la même fonction (`snapcast.ws_target`) : c'est ce qui
garantit qu'ils visent le même serveur avec le même schéma, un `ws://` codé en
dur d'un côté suffisant à casser tout le mode TLS.

Une connexion par appel, volontairement : le contrôle est peu sollicité, et
cela évite un thread lecteur, une machine à états de reconnexion et un cache à
invalider. Snapserver entrelaçant ses notifications avec les réponses, chaque
réponse est retrouvée par son `id`.

### Autorité de certification interne

Sur un réseau airgap, les certificats sont signés par une autorité maison
qu'aucun magasin livré avec les images ne connaît. **`TLS_CA_FILE`** la déclare
une fois pour toutes les connexions chiffrées sortantes de l'API :

| Client | Ce qu'il joint | Comment la variable est appliquée |
|---|---|---|
| boto3 | Endpoint S3 (listage, téléchargement, signature) | `verify=` |
| ffmpeg | URL présignée du morceau, en lecture Snapcast | `-tls_verify 1 -ca_file` |
| websockets | Le proxy TLS devant snapserver | `ssl.create_default_context(cafile=)` |

Le chemin est lu **dans le conteneur de l'API** : monter le fichier, par
exemple `- /etc/pki/interne.pem:/etc/ssl/interne.pem:ro`.

Deux pièges :

- ce fichier **remplace** le magasin système, il ne s'y ajoute pas. Pour
  joindre à la fois des serveurs internes et des serveurs à certificat public,
  y concaténer les deux jeux d'autorités ;
- **ffmpeg ne vérifie rien par défaut** (`tls_verify` vaut `0`). Un endpoint S3
  en `https` était donc accepté quel que soit son certificat, alors même que
  l'URL présignée qui y transite porte les droits de lecture du bucket. La
  vérification est désormais activée dès que l'URL est chiffrée — un stockage
  `https` à certificat auto-signé et sans `TLS_CA_FILE` cessera de fonctionner,
  ce qui est le comportement voulu.

### TLS vers snapserver

Snapserver ne chiffre rien lui-même : `SNAPCAST_TLS=true` suppose un **reverse
proxy TLS devant lui**, avec `SNAPCAST_HTTP_PORT` pointant sur ce proxy. Les
deux canaux passent alors en `wss://`.

| Variable | Rôle |
|---|---|
| `SNAPCAST_TLS` | Passe le contrôle et l'audio en `wss://` |
| `SNAPCAST_TLS_CA_FILE` | Surcharge de `TLS_CA_FILE`, si le proxy de snapserver est signé par une autre autorité |
| `SNAPCAST_TLS_SERVER_NAME` | Nom vérifié contre le certificat, et SNI. Vide = `SNAPCAST_HOST` |

Le certificat est toujours vérifié — il n'y a volontairement pas d'option pour
désactiver ce contrôle : un contrôle non authentifié laisserait piloter les
enceintes par n'importe quel serveur répondant sur le port. En airgap, la
réponse est de renseigner `TLS_CA_FILE` (voir ci-dessous). Renseigner
`SNAPCAST_TLS_SERVER_NAME` quand on joint le serveur par IP mais que le
certificat porte un nom.

**Ces trois réglages ne sont pas modifiables à chaud** depuis l'écran
Configuration, contrairement à l'hôte, au port et à l'adresse annoncée : ils
sont lus dans l'environnement, donc changés par redéploiement.

### Vocabulaire : un groupe n'est pas une pièce

Snapcast connaît trois objets, et aucun ne représente un lieu :

| Objet | Ce que c'est |
|---|---|
| **Client** | Un appareil — c'est *lui* qui se trouve dans une pièce |
| **Groupe** | Un ensemble d'appareils qui jouent **le même flux, synchronisés** |
| **Flux** | Une source audio — ici, une session d'écoute |

Un groupe est donc une **zone de diffusion**, pas un lieu : y réunir le salon et
la cuisine leur fait jouer la même chose, ce qui est le cas d'usage courant.

### Sessions

Une **session d'écoute** est une file d'attente partagée : chacun la crée ou la
rejoint librement, y ajoute des titres ou des albums, et voit qui a ajouté quoi.
La file est une playlist ordonnée avec un pointeur sur l'élément courant : les
titres joués ne sont pas consommés, ce qui donne gratuitement le retour arrière,
l'historique et la relecture.

Rejoindre une session, c'est se synchroniser via Snapcast : dès sa création,
chaque session publie son propre flux (`Stream.AddStream`), que le serveur lit
et diffuse, à assigner aux zones de diffusion voulues. Un navigateur qui la
rejoint peut aussi l'écouter lui-même, en synchronisation. Il n'y a pas de
sortie « locale » par session : une écoute purement locale, hors
synchronisation, n'implique aucune session — c'est simplement le navigateur qui
lit sa propre file, sans que personne d'autre n'y ait accès.

Tout le monde peut agir sur la lecture d'une session pour l'instant ; les
rôles viendront se greffer sur `CurrentUser`, déjà injecté dans chaque route.

### Configuration requise côté Snapcast

Rien à modifier dans `snapserver.conf` : chaque session enregistre son flux à la
chaud via `Stream.AddStream`, en `mode=client` — c'est **snapserver qui vient se
connecter** à un port ouvert par l'API. Il faut donc que :

- `SNAPCAST_HOST` et `SNAPCAST_HTTP_PORT` désignent le serveur HTTP de
  snapserver, vu depuis l'API (contrôle *et* audio, voir plus haut) ;
- `SNAPCAST_ADVERTISE_HOST` désigne l'adresse de l'API **vue depuis snapserver** ;
- la plage `SNAPCAST_PORT_START`…`_COUNT` soit joignable depuis lui.

Attention au sens de chaque flèche : les deux premiers réglages disent comment
**joindre** snapserver, les deux suivants comment snapserver **nous** joint.

Pièges rencontrés à l'implémentation, et traités dans le code :

- snapserver **exige une adresse IP** dans l'URI en `mode=client` ; un nom
  d'hôte est rejeté par un laconique « Invalid argument ». L'hôte configuré est
  donc résolu en IP avant d'être transmis.
- un `Stream.AddStream` en échec **laisse malgré tout le nom enregistré** : on
  retire systématiquement avant d'ajouter.
- snapserver **entrelace ses notifications avec les réponses** sur la même
  connexion ; les réponses sont corrélées par `id`, le reste est ignoré.
- l'en-tête binaire fait **26 octets** ; s'y tromper ne produit pas une erreur
  propre mais **fait segfauter snapserver 0.29** (constaté). Un test verrouille
  cette valeur.
- snapserver émet `size` = taille du **corps seul**, alors que Snapweb envoie la
  taille totale. Sur WebSocket le champ ne délimite rien, d'où la tolérance ; on
  suit la spécification.
- l'horloge de snapserver **ne part pas de l'epoch** mais de son démarrage. Le
  calcul de décalage l'absorbe, à condition de ne jamais supposer le contraire.

L'image Docker de l'API embarque **ffmpeg** (~250 Mo) : le mode s'active à
chaud, il doit donc être présent même s'il ne sert pas.

### Identité

C'est le pseudo, ou l'identité OIDC, qui s'affiche à côté des titres ajoutés à
une file partagée — voir [Identité et OIDC](#identite-et-oidc). Le nom du
snapclient qu'est le navigateur vient de la même source, jamais d'un
renommage indépendant.

## Identite et OIDC

Deux modes, choisis par `OIDC_ENABLED`.

**Sans OIDC** (defaut), l'identite se resume au pseudo saisi dans l'ecran
Configuration et transmis dans l'en-tete `X-User-Name`. Rien n'est verifie :
ce mode convient au developpement et a un poste isole.

**Avec OIDC**, l'identite vient d'un jeton valide par le fournisseur.
La boite de saisie du pseudo disparait, et **`X-User-Name` cesse d'etre lue** —
la laisser active offrirait un chemin trivial pour se faire passer pour
quelqu'un d'autre, ce qui viderait l'authentification de son sens.

### Ce qui est mis en oeuvre

Code d'autorisation avec **PKCE**, l'API jouant le **client confidentiel**.
Le navigateur ne recoit jamais de jeton, seulement un cookie de session signe.
Ce n'est pas un detail d'implementation : le relais audio est une **WebSocket**,
qui ne peut pas porter d'en-tete `Authorization` depuis le navigateur — mais
qui porte les cookies.

Aucun jeton n'est conserve apres la connexion, seulement l'identite validee.
Il n'y a donc ni rafraichissement ni jeton au repos ; la session applicative a
sa propre duree (`SESSION_MAX_AGE_S`), au terme de laquelle il faut se
reconnecter.

Seule l'URL de l'emetteur est configuree : les points d'entree sont lus dans
son document de decouverte. **Authentik, Keycloak, Dex, Zitadel ou Entra se
branchent donc de la meme facon.**

| Variable | Role |
|---|---|
| `OIDC_ENABLED` | Bascule entre les deux modes |
| `OIDC_ISSUER` | URL de l'emetteur, **sans** `/.well-known` |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | Le client declare chez le fournisseur |
| `OIDC_SCOPES` | `openid` obligatoire ; `groups` pour recevoir les groupes |
| `OIDC_GROUPS_CLAIM` | Revendication portant les groupes (`groups` chez Authentik) |
| `OIDC_ADMIN_GROUP` | Groupe donnant le role admin. Vide = personne |
| `OIDC_CA_FILE` | Surcharge de `TLS_CA_FILE` pour le seul fournisseur |
| `SESSION_SECRET` | **Obligatoire** si OIDC est actif. `openssl rand -hex 32` |
| `PUBLIC_BASE_URL` | Sert a construire l'URI de redirection |

**URI de redirection a declarer chez le fournisseur** :
`<PUBLIC_BASE_URL>/api/auth/callback`, au caractere pres.

### Certificats

Le fournisseur est joint avec le magasin de `TLS_CA_FILE` (voir plus haut), ou
celui d'`OIDC_CA_FILE` s'il est signe par une autre autorite. La verification
n'est jamais desactivable : un fournisseur d'identite usurpe permettrait de
forger n'importe quelle connexion.

Un certificat refuse le dit clairement, en nommant le reglage a poser :

```
decouverte OIDC impossible sur https://idp.interne/.well-known/openid-configuration :
le certificat du fournisseur est refuse (unable to get local issuer certificate).
Sur un reseau airgap, renseigner OIDC_CA_FILE ou TLS_CA_FILE avec l'autorite
qui l'a signe.
```

### Roles

Le role est calcule a chaque requete depuis les groupes du jeton : le
fournisseur reste la source de verite, il n'y a pas de table d'utilisateurs a
tenir a jour. **Aucune route n'est encore restreinte** — les roles sont lus et
affiches, leur application viendra.

Sans `OIDC_ADMIN_GROUP`, personne n'est administrateur : un defaut qui
donnerait ce role sur une configuration incomplete serait le mauvais sens de
securite.

### Verification

`scripts/check_oidc.sh` (depot d'orchestration) rejoue le flux complet contre
**Dex**, servi en TLS avec une autorite maison. Dex n'est pas Authentik, et
c'est le but : si le flux passe la, c'est qu'il ne depend d'aucune
particularite du fournisseur. Le script verifie aussi qu'**un certificat non
approuve fait echouer la connexion**.

## Genre et paroles

Les deux sont lus dans les tags, jamais devinés — rien n'est récupérable en
airgap si le fichier ne les porte pas.

**Genre** — stocké sur la piste (`tracks.genre`, indexé). Le genre d'un album
est *agrégé* depuis ses pistes plutôt que dupliqué dans une colonne.

**Paroles** — `tracks.lyrics` est une colonne **différée** : elle n'est jamais
remontée par les listes de pistes, qui n'exposent qu'un booléen `has_lyrics`
calculé en SQL. Le texte est servi à la demande par
`GET /api/tracks/{id}/lyrics`. La lecture couvre les trois conventions : frame
`USLT` en ID3, atome `©lyr` en MP4, commentaire `LYRICS`/`UNSYNCEDLYRICS` en
Vorbis. Le contenu est tronqué à 20 000 caractères.

Ni l'un ni l'autre n'est éditable manuellement : ce sont des valeurs de tag, pas
des corrections de catalogue.

## Compatibilité avec zimmporter

Le bucket est alimenté par [zimmporter](https://github.com/Tomifarmer/zimmporter-api),
qui écrit ses tags avec mutagen avant l'upload. Conventions constatées, figées
par `tests/test_tags_zimmporter.py` :

- fichiers **AAC/m4a**, clé `{artist}/{album}/{NN - titre}.m4a` (les `/` des noms
  sont remplacés par des `-`) ;
- tags écrits : `title`, `artist`, `album`, `date`, `tracknumber`, `genre`
  (iTunes), paroles LRCLIB (`©lyr`), pochette dans l'atome `covr` ;
- **ni `albumartist` ni `discnumber`** — l'identité de l'album retombe donc sur
  `artist`, ce qui regroupe correctement les albums ;
- **les playlists forcent `artist` à la chaîne littérale `"playlists"`** et
  n'écrivent aucun numéro de piste. Elles apparaissent donc dans le catalogue
  sous un artiste nommé « playlists », et l'artiste réel de chaque morceau
  n'existe nulle part dans le fichier.

## Points d'attention

- **Le scan tourne dans le process de l'API** (pas de Celery ni de Redis) ;
  un verrou en base interdit deux scans simultanés (409 sinon).
- **L'authentification est prête pour OIDC/Authentik** : toutes les routes
  injectent déjà `CurrentUser`, et seule l'implémentation de `app/auth.py` sera
  à remplacer.
- **Formats lus** : mp3, flac, m4a/mp4, ogg/opus, wav, wma. Aucun transcodage —
  le navigateur doit savoir décoder le format.

## Licence

MIT, voir [`LICENSE`](./LICENSE).
