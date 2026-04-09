# 🤖 Polymarket BTC 5-Min Bot

Bot de trading simulé (DRY RUN) sur les marchés **BTC Up/Down 5 minutes** de Polymarket.

## Stratégie

Les marchés BTC 5-min de Polymarket ouvrent une nouvelle fenêtre toutes les 5 minutes  
alignée sur l'heure Unix (ex: 14:00:00, 14:05:00, 14:10:00...).

Le bot :
1. **Surveille le prix BTC** en temps réel via Binance WebSocket
2. **Calcule le delta** depuis l'ouverture de la fenêtre (ex: +0.05%)
3. **Dans les 30 dernières secondes** avant clôture, si le delta dépasse le seuil → trade
4. **Simule le résultat** et met à jour le bankroll

## Logique de signal

```
delta BTC depuis ouverture > MIN_CONFIDENCE_PCT  →  parie YES (BTC UP)
delta BTC depuis ouverture < -MIN_CONFIDENCE_PCT →  parie NO  (BTC DOWN)
delta trop faible                                →  skip
```

## Installation locale

```bash
git clone <repo>
cd btc5m-bot
pip install -r requirements.txt
cp .env.example .env
# Édite .env si besoin
python bot.py
```

## Déploiement Railway.app

1. Push ce dossier sur GitHub
2. Dans Railway → New Project → Deploy from GitHub
3. Ajouter les variables d'environnement depuis `.env.example`
4. Railway détecte automatiquement `Procfile` → lance `python bot.py`

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `DRY_RUN` | `true` | Mode simulation (pas de vrai argent) |
| `STARTING_BANKROLL` | `300.0` | Bankroll simulée en USD |
| `BET_SIZE_PCT` | `0.05` | 5% du bankroll par trade |
| `MIN_BET` | `2.0` | Mise minimum en USD |
| `MAX_BET` | `25.0` | Mise maximum en USD |
| `ENTRY_SECONDS_BEFORE` | `30` | Entrer dans les X dernières secondes |
| `MIN_CONFIDENCE_PCT` | `0.03` | Delta BTC minimum en % pour déclencher un trade |
| `TELEGRAM_TOKEN` | *(vide)* | Token bot Telegram (optionnel) |
| `TELEGRAM_CHAT_ID` | *(vide)* | Chat ID Telegram (optionnel) |

## Modèle de prix token (simulation)

Le prix simulé du token reflète la probabilité implicite basée sur le delta BTC :

| Delta BTC | Prix token gagnant |
|---|---|
| < 0.005% | $0.50 (coin flip) |
| ~0.02% | $0.55 |
| ~0.05% | $0.65 |
| ~0.10% | $0.80 |
| ≥ 0.15% | $0.92–0.97 |

## Structure des fichiers

```
btc5m-bot/
├── bot.py            # Bot principal
├── requirements.txt  # Dépendances Python
├── .env.example      # Template variables env
├── Procfile          # Pour Railway.app
├── railway.toml      # Config Railway
└── README.md         # Ce fichier
```

## Passage en LIVE (quand tu seras prêt)

Pour trader en live, il faudra :
1. `DRY_RUN=false` dans `.env`
2. Ajouter les clés API Polymarket (CLOB)
3. Intégrer `py-clob-client` pour la signature d'ordres
4. Activer les allowances USDC sur Polygon

⚠️ **Toujours tester en DRY RUN en premier !**
