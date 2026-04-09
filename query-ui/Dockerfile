FROM node:20-alpine AS build
WORKDIR /app
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG VITE_KEYCLOAK_URL=http://localhost:8080
ENV VITE_KEYCLOAK_URL=$VITE_KEYCLOAK_URL
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
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
