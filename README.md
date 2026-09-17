# FFBB → calendrier ICS (Jules + Bastien)

Ce projet récupère les calendriers d'équipes depuis competitions.ffbb.com, suit les liens
de rencontre associés à `versus.svg`, récupère la salle et l'adresse, puis publie un
calendrier `.ics` utilisable par Apple Calendrier / Google Agenda.

## Configuration initiale

Les deux équipes de la phase 1 sont déjà configurées :

- Jules → équipe FFBB `200000005368033`
- Bastien → équipe FFBB `200000005367465`

Les URLs des phases 2 et 3 pourront être ajoutées plus tard dans `config.yaml`.
Une phase n'est jamais supprimée automatiquement : les anciens événements restent dans
le calendrier.

## Déploiement recommandé : GitHub Actions + GitHub Pages

1. Créer un dépôt GitHub (idéalement privé).
2. Copier tous les fichiers de ce dossier dans le dépôt.
3. Activer GitHub Pages avec la source `GitHub Actions`.
4. Dans le dépôt, aller dans **Settings → Pages** et vérifier que Pages est activé.
5. L'action `.github/workflows/update-calendar.yml` s'exécute :
   - à chaque push ;
   - automatiquement toutes les 6 heures ;
   - manuellement via **Actions → Update calendar → Run workflow**.
6. Après la première exécution, le fichier sera publié à :
   `https://<utilisateur>.github.io/<depot>/basket.ics`
7. Sur le téléphone, s'abonner à cette URL comme calendrier distant.

### Confidentialité

Un calendrier ICS publié sur GitHub Pages est accessible à toute personne qui connaît
l'URL. Il contient des horaires et lieux de matchs d'enfants. Pour un usage familial,
préférer un dépôt privé et/ou un hébergement privé compatible avec les abonnements
calendrier. GitHub Pages peut avoir des restrictions selon le type de compte et de dépôt.

## Ajouter une phase

Dans `config.yaml`, ajouter simplement une nouvelle URL sous l'enfant concerné :

```yaml
children:
  - name: Jules
    phases:
      - name: phase-1
        url: "..."
      - name: phase-2
        url: "NOUVELLE_URL"
      - name: phase-3
        url: "NOUVELLE_URL"
```

Le programme fusionne toutes les phases et utilise l'identifiant de rencontre FFBB
comme identifiant stable lorsqu'il est disponible. Ainsi, un changement d'horaire ou
de salle met à jour l'événement existant au lieu de créer un doublon.

## Tester localement

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python ffbb_sync.py
```

Le fichier `basket.ics` sera créé dans le dossier courant.

## Format des événements

Titre :
`Match basket Jules`

Lieu :
`Nom de la salle, adresse`

Description :
- enfant
- équipe
- journée
- adversaire
- domicile/extérieur
- lien FFBB de la rencontre
- phase

La durée par défaut est de 1 h 30. Elle est réglable dans `config.yaml`.
