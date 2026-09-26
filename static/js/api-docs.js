const mount = document.querySelector("#swagger-ui");

if (mount && window.SwaggerUIBundle) {
  window.SwaggerUIBundle({
    url: "/api/v1/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    displayRequestDuration: true,
    filter: true,
    persistAuthorization: false,
    tryItOutEnabled: false,
  });
}
