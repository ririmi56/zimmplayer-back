# Zimmplayer — API

Backend d'un lecteur de musique auto-hébergé pour une bibliothèque stockée sur
S3/MinIO, conçu pour fonctionner sur un **réseau airgap** : aucune dépendance
réseau externe à l'exécution. Le client web correspondant vit dans le dépôt
sœur [`zimmplayer-front`](https://github.com/ririmi56/zimmplayer-front).

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

- `SNAPCAST_ADVERTISE_HOST` désigne l'adresse de l'API **vue depuis snapserver** ;
- la plage `SNAPCAST_PORT_START`…`_COUNT` soit joignable depuis lui.

Pièges rencontrés à l'implémentation, et traités dans le code :

- snapserver **exige une adresse IP** dans l'URI en `mode=client` ; un nom
  d'hôte est rejeté par un laconique « Invalid argument ». L'hôte configuré est
  donc résolu en IP avant d'être transmis.
- un `Stream.AddStream` en échec **laisse malgré tout le nom enregistré** : on
  retire systématiquement avant d'ajouter.
- snapserver **entrelace ses notifications avec les réponses** sur la socket de
  contrôle ; les réponses sont corrélées par `id`, le reste est ignoré.
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

Sans authentification, l'identité se résume à un pseudo choisi côté client,
envoyé en en-tête `X-User-Name`. C'est ce pseudo qui s'affiche à côté des
titres ajoutés. À l'arrivée d'OIDC, seul `app/auth.py` change — toutes les
routes injectent déjà `CurrentUser`.

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
