cd /opt/aldiapp/app
ls -la
source /opt/aldiapp/app/.venv/bin/activate
gunicorn -w 1 -b 127.0.0.1:5000 app:app
sudo ss -lntp | grep :5000
exit
