#!/bin/bash
# Indexation de toute la France (france.py), en autonomie :
#  - caffeinate empêche le Mac de se mettre en veille ;
#  - relance après 30 s en cas d'arrêt inattendu, après 1 h s'il reste des cases en échec ;
#  - quand tout est calculé, met le site à jour (deployer.sh).
# Arrêt : pkill -TERM -f lancer-france; pkill -TERM -f "caffeinate -i bash"; pkill -TERM -f france.py
cd "$(dirname "$0")" || exit 1
mkdir -p logs
exec caffeinate -i bash -c '
  while true; do
    python3 france.py "$@"
    code=$?
    case $code in
      0)   echo "$(date "+%F %T") France terminée : déploiement" ; ./deployer.sh ; exit 0 ;;
      130) exit 130 ;;
      2)   echo "$(date "+%F %T") Des cases restent en échec, nouvelle série dans 1 h" | tee -a logs/france.log ; sleep 3600 ;;
      *)   echo "$(date "+%F %T") Arrêt inattendu (code $code), relance dans 30 s" | tee -a logs/france.log ; sleep 30 ;;
    esac
  done
' _ "$@"
