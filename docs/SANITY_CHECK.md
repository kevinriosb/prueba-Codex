# Revisión general — 27 de octubre de 2026

Se revisaron backend, herramientas, almacenamiento, Telegram, navegador, frontend,
scripts administrativos y configuración de despliegue y pruebas. La revisión se
realizó sobre una copia de desarrollo, con servicios y documentos ficticios.

## Fallos reproducidos y corregidos

| Prioridad | Caso observado | Corrección |
| --- | --- | --- |
| Alta | Un correo con dos XML de UUID distintos podía asociar ambos a la misma compra. | Comprobar un único UUID consistente con el ticket y entre todos los XML del correo, antes de guardar adjuntos. |
| Media | Un PDF seguido de un XML inválido dejaba un archivo huérfano y el correo marcado como procesado; una revisión posterior lo saltaba. | Validar todos los adjuntos antes de guardar los archivos o marcar el correo como procesado. |
| Media | Una nueva consulta SAT negativa conservaba `sat_verified=true` de una consulta anterior. Un PDF recibido después podía completar incorrectamente el trámite. | Actualizar el resultado y la fecha en cada respuesta SAT, también si no confirma vigencia. Un resultado anterior no sustituye al nuevo. |
| Media | Autorizar el destino principal de un portal no permitía continuar si quedaba otro recurso auxiliar sin permiso. | Separar el dominio pendiente de la navegación principal de los recursos auxiliares. Autorizar un recurso auxiliar sigue sin autorizar el destino principal. Los bloqueos por POST o límites permanecen. |

Se añadieron seis casos de regresión. Los cinco casos iniciales fallaron antes de
las correcciones; el sexto comprueba que autorizar solo el recurso auxiliar no
desbloquee un destino principal sin permiso. El caso del SAT cubre tanto una
respuesta negativa como una respuesta que no confirme la consulta.

La validación previa de adjuntos evita escrituras parciales por documentos
inválidos. No convierte todas las operaciones de almacenamiento en una única
transacción frente a una caída del proceso o un disco lleno.

## Comprobaciones

- **521 pruebas de la suite completa pasaron** (154,41 segundos), incluyendo autenticación, CSRF, propiedad por
  usuario, cifrado, consentimientos, cuotas, deduplicación y permisos del navegador.
- Chromium real con páginas ficticias: navegación, destinos nuevos y recursos
  auxiliares, sin enviar datos fiscales ni acceder a comercios.
- Interfaz de chat y facturación en emulación de iPad y móvil: inicio y cierre de
  sesión, persistencia, cálculo, revisión/rechazo/envío simulado y texto no ejecutable.
- Imágenes finales de `agent` y `browser` construidas; pruebas Docker/Compose de
  salud, autenticación interna, aislamiento, rechazo de archivos locales y
  recuperación del historial y documento cifrado al recrear el contenedor con
  el mismo volumen. Los recursos de prueba se eliminaron al terminar.
- Compilación de Python y sintaxis de ambos archivos JavaScript.
- Ruff para errores sintácticos y símbolos sin definir; comprobación de
  compatibilidad de los paquetes instalados.
- `pip-audit`: 61 dependencias consultadas, sin paquetes omitidos ni vulnerabilidades
  conocidas reportadas por la base consultada en la fecha de esta revisión.
- Bandit: cuatro avisos revisados. La advertencia sobre XML corresponde a
  `xml.sax.saxutils.escape`, que escapa texto de salida; el análisis entrante usa
  `defusedxml`. El `except/pass` descarta una etiqueta HTML no compatible. Los dos
  avisos de `/tmp` corresponden al entorno del navegador aislado y su almacenamiento
  temporal privado en Docker; no se escriben credenciales allí.

## Límites de la revisión

Las respuestas de IA, Gmail y SAT son simuladas. La revisión no prueba saldo,
conectividad o permisos reales de esos proveedores, ni compatibilidad universal
con portales externos. No es una prueba de penetración del VPS. El navegador
actual sigue limitado a revisión pública: no rellena datos fiscales ni solicita
facturas por portales.

No se accedió al VPS, no se publicaron facturas ni se enviaron correos reales.
Para aplicar las correcciones al servidor se necesita actualizar y reconstruir
los servicios `agent` y `browser`, conservando sus variables y volúmenes existentes.

Desde Termius:

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
