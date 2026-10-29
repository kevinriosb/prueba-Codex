# Conservar el formulario y observar los selectores

La captura del VPS vuelve al selector de marcas y muestra un límite de ejecución.
No demuestra un problema de CSF o legibilidad. La descarga del HTML real desde
este entorno fue rechazada por el proxy (403); el diagnóstico del VPS sigue siendo
necesario para confirmar el motivo exacto de sus opciones ausentes.

Se reprodujeron cuatro fallos antes del cambio, con Chromium real y páginas
ficticias: enlaces sin texto cuyo nombre de marca aparece en un parámetro público,
botones de navegación explícitos dentro de formularios, controles que aparecen
después de la primera observación y pérdida del formulario al caducar el permiso.

## Cambios

- Un formulario con URL pública estable observada se conserva cifrado para ese
  ticket hasta siete días (según `BILLING_TICKET_RETENTION_HOURS`). Su URL, perfil
  y origen están vinculados al alcance del ticket. No guarda cookies o sesiones.
- Renovar el permiso público de 30 minutos permite abrir ese formulario; guardar
  la ruta no amplía el permiso. Un selector sin opciones no reemplaza el mejor
  formulario alcanzado. Cambiar perfil, origen o selección invalida ese avance.
- No hay una receta por establecimiento. Se reconocen etiquetas SVG, atributos
  públicos de marca y nombres literales en parámetros como `brand`, `marca` u
  `opc`, solo dentro de un selector explícito. Se usa el enlace observado completo;
  no se construye un destino a partir del nombre del ticket.
- Un botón `type="button"` con navegación literal puede estar dentro de un form.
  El navegador sigue su URL con GET sin ejecutar el manejador. Los botones de
  submit y el JavaScript arbitrario siguen excluidos.
- Una observación de selector vacío espera una sola vez hasta 1,2 segundos por
  sus controles. La espera renueva las referencias DOM; no abre otra ejecución de
  IA ni modifica los presupuestos de red.
- Si sigue sin existir una opción clara, la revisión termina con elección humana
  o diagnóstico. No consume turnos hasta mostrar un límite que oculte ese estado.

El avance automático es del ticket actual. **Guardar portal** mantiene su función
separada: confirmar una dirección para futuros tickets del comercio. Las rutas
anteriores ya borradas o reemplazadas no pueden recuperarse de una captura.

## Comprobar en el VPS

Actualizar y reconstruir `agent` y `browser` con los dos archivos Compose; véase
[navegación](NAVEGACION.md#activar-desde-termius). Después, `/estado` y **Permitir**
o **Revisar portal con IA**. El perfil, los tickets y sus cuotas se conservan.

Si el selector aún no presenta opciones, **Ver diagnóstico (sin IA)** devuelve
el informe guardado sin repetir OpenAI. La inspección pública por consola puede
mostrar también controles sin imágenes, SVG y botones:

```bash
(
  set -e
  cd /opt/lilyapp-agent
  read -r -p "URL pública del portal: " portal_url
  read -r -p "Marca impresa en el ticket: " merchant_name
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml exec -T browser python - \
    --url "$portal_url" --merchant "$merchant_name" --layout < scripts/diagnose_portal.py
)
```

El diagnóstico abre páginas públicas nuevas; no usa documentos, perfiles, cuotas
de IA ni valores de compra. No envía formularios. No imprime cookies, claves,
manejadores JavaScript o consultas de las direcciones de los controles.
