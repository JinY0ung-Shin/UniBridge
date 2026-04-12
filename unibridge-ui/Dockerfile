FROM node:20-alpine AS build
WORKDIR /app
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG HOST_IP=localhost
ARG KEYCLOAK_PORT=8443
ARG VITE_KEYCLOAK_URL
ARG VITE_KEYCLOAK_REALM=apihub
ARG VITE_KEYCLOAK_CLIENT_ID=apihub-ui
ARG VITE_LITELLM_PORT=4000
ENV VITE_KEYCLOAK_URL=${VITE_KEYCLOAK_URL:-https://${HOST_IP}:${KEYCLOAK_PORT}}
ENV VITE_KEYCLOAK_REALM=$VITE_KEYCLOAK_REALM
ENV VITE_KEYCLOAK_CLIENT_ID=$VITE_KEYCLOAK_CLIENT_ID
ENV VITE_LITELLM_PORT=$VITE_LITELLM_PORT
COPY package*.json ./
RUN npm config set strict-ssl false && \
    if [ -n "$HTTP_PROXY" ]; then npm config set proxy $HTTP_PROXY; fi && \
    if [ -n "$HTTPS_PROXY" ]; then npm config set https-proxy $HTTPS_PROXY; fi && \
    npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 443
