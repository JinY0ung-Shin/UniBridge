import Keycloak from 'keycloak-js';

const runtimeConfig = window.__RUNTIME_CONFIG__;

const keycloak = new Keycloak({
  url: runtimeConfig?.KEYCLOAK_URL || import.meta.env.VITE_KEYCLOAK_URL || 'http://localhost:8080',
  realm: runtimeConfig?.KEYCLOAK_REALM || import.meta.env.VITE_KEYCLOAK_REALM || 'apihub',
  clientId: runtimeConfig?.KEYCLOAK_CLIENT_ID || import.meta.env.VITE_KEYCLOAK_CLIENT_ID || 'apihub-ui',
});

export default keycloak;
