# Вариант «в докере» — удобен, если на сервере уже есть Docker (или для Amvera/VK Cloud/Selectel).
# Сборка и запуск:
#   docker build -t potok-web .
#   docker run -d --name potok --restart unless-stopped -p 80:80 potok-web
FROM nginx:1.27-alpine

COPY index.html /usr/share/nginx/html/index.html
COPY nginx-docker.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
