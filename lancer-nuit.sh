#!/bin/bash
# Lance le pré-calcul en autonomie :
#  - caffeinate empêche le Mac de se mettre en veille tant que le calcul tourne ;
#  - si le script Python s'arrête de façon inattendue, il est relancé après 30 s ;
#  - tout est journalisé dans logs/precompute.log.
# Exemples :
#   ./lancer-nuit.sh                                   # 50 km autour de Paris
#   ./lancer-nuit.sh --lat 45.764 --lon 4.835 --radius 30
# Arrêt : Ctrl+C.
cd "$(dirname "$0")" || exit 1
mkdir -p logs
exec caffeinate -i bash -c '
  while true; do
    python3 precompute.py "$@"
    code=$?
    case $code in
      0)   echo "$(date "+%F %T") Terminé." ; exit 0 ;;
      130) exit 130 ;;                       # Ctrl+C
      2)   echo "$(date "+%F %T") Des cases restent en échec, nouvelle série dans 1 h" | tee -a logs/precompute.log ; sleep 3600 ;;
      *)   echo "$(date "+%F %T") Arrêt inattendu (code $code), relance dans 30 s" | tee -a logs/precompute.log ; sleep 30 ;;
    esac
  done
' _ "$@"
