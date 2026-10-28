# Un permiso para el recorrido público del ticket

La corrección posterior del POST auxiliar y la prioridad del selector de marcas
está documentada en [selector de marcas](SELECTOR_MARCAS.md).

La captura llegó a `alsea.interfactura.com/RegistroDocumento.aspx`, pero aún pedía
elegir la marca y autorizar bibliotecas/estilos. Había dos barreras de implementación:
permiso por origen y un solo intento de navegación por ejecución.

Telegram ahora solicita un consentimiento explícito para seguir el recorrido
público de ese ticket, incluidos proveedores y recursos funcionales. Un permiso
antiguo por dominio no se amplía automáticamente. Sigue separado del análisis del
documento, envío de datos fiscales, correo y emisión de factura.

El mismo agente usa controles observados. Sigue enlaces inequívocos de facturación,
elige una marca solo si coincide de forma única y reevalúa cada página. Estos pasos
claros no agregan llamadas de IA. No contiene recetas de Domino’s, Alsea o REA.
Ante ambigüedad ofrece la elección humana. Al encontrar el formulario se detiene;
guardar su enlace estable sigue requiriendo el botón **Guardar portal**.

Se conservan las cuotas/turnos/tokens y el plazo de ejecución existentes. Un trabajo
admite hasta siete clics públicos; la sesión mantiene ocho pasos contando la apertura,
12 orígenes, 60 solicitudes por página, 240 por recorrido y 6 MiB. Los intentos auxiliares denegados también
cuentan. El avance automático tiene un presupuesto de 22 segundos
para iniciar pasos, sin repetir un clic fallido. No se abre otro trabajo automáticamente
al agotar el presupuesto. La API web mantiene sus permisos por dominio.

La recuperación del avance del mismo ticket y sus condiciones están descritas en
[límites y recuperación](LIMITES_RECORRIDO.md).

Los recursos siguen pasando por validación DNS pública, conexión a IP fijada y TLS.
Los scripts/estilos pueden seguir hasta tres redirecciones adicionales validadas,
sin reenviar cookies del origen anterior. Se omiten medición, imágenes y fuentes.
POST, WebSockets, descargas, redes privadas, CAPTCHA y operaciones privadas no
quedan habilitados por el consentimiento público.

## Actualizar en Termius

```bash
(
  set -e
  cd /opt/lilyapp-agent
  git pull --ff-only
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml build agent browser
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml up -d --wait --wait-timeout 180
  curl --fail --silent --show-error --max-time 20 https://agente.lilyapp.mx/health
)
```

Después: `/estado` en Telegram, **Revisar portal con IA** y el nuevo **Permitir**
que explica el recorrido completo. No es necesario volver a subir CSF ni ticket.
Si ya había una sesión con permisos antiguos, la autorización nueva habilita su
recorrido y recarga los recursos pendientes sin borrar datos.

## Verificación

Las regresiones usan el SDK real y Chromium con páginas/servicios simulados:
enlace de facturación → selector de marcas → proveedor externo → formulario,
recursos funcionales de otros orígenes, aislamiento, permisos antiguos y caducidad.
No usan OpenAI, correo o SAT reales ni emiten facturas.

La suite completa pasó con 550 pruebas (151,71 segundos). También pasaron las dos
variantes del contrato del cliente (permiso por dominio/recorrido), Ruff, compilación
y el arranque de ambas imágenes y del stack Compose con autenticación interna.
Se verificaron además redirecciones de recursos sin fuga de cookies, bloqueo de
DNS privado/POST y el máximo de orígenes y clics. Los recursos de prueba se retiraron.

La conexión a la página real de Interfactura desde el entorno de desarrollo fue
rechazada por su proxy (CONNECT 403). El resultado real del formulario en el VPS
se debe verificar después de actualizar; las pruebas locales no demuestran
compatibilidad con todos los portales.
