# Solicitud de factura desde Telegram

El recorrido público y el llenado aislado existentes conservan su comportamiento.
El botón **Continuar facturación** abre un canal privado nuevo para enviar los
formularios. Revisar los datos no transmite el RFC ni solicita una factura.

1. El navegador identifica los campos y los botones reales de esa pantalla.
2. Telegram presenta los valores y los dominios que los recibirán. **Confirmar:
   Buscar/Continuar/Generar factura** autoriza únicamente ese paso.
3. Se llena y pulsa ese control. Si el portal muestra otra pantalla, se vuelven
   a obtener sus campos. Los valores conocidos se toman del perfil y del ticket.
4. Un campo pendiente ofrece **Completar campo**: escribir el valor o enviar
   una fotografía cercana de ese campo. El recorte se lee solo tras autorizar
   OpenAI y su resultado requiere **Usar dato leído**. Un recorte nunca sustituye
   al ticket completo ni abre otra solicitud. Un valor ilegible permanece vacío.
5. El envío final recoge los enlaces XML/PDF observados del proveedor. Se guardan
   cifrados y se cotejan RFC, nombre, CP, régimen, uso, importe, moneda y emisor
   con el CFDI. La consulta al SAT conserva su permiso separado.

No hay una receta por marca, selectores de Dominós ni un dominio fijo. Se admiten
formularios HTML GET/POST, botones con JavaScript y respuestas con nuevos campos.
Los controles ambiguos, autenticación, CAPTCHA, adjuntos obligatorios o widgets
sin soporte se presentan como intervención manual. Esto no acredita compatibilidad
con todos los comercios. Los PDF/XML disponibles se entregan por Telegram; si el
comercio solo envía por correo, se espera su envío al correo revisado.

## Envíos e incertidumbre

- Cada permiso vincula propietario, solicitud activa, documento, perfil, valores,
  sesión, observación, control y destinos. Caduca a los 15 minutos. Cambiar un
  dato invalida su confirmación previa.
- La autorización se reclama antes del efecto. El worker reserva el identificador
  antes de rellenar/clicar y conserva el resultado. Un doble clic no repite el POST.
- Tras perder la respuesta, **Consultar envío** recupera la evidencia en memoria
  sin clicar ni enviar de nuevo. Si no se puede confirmar, conserva **Envío sin
  confirmar** y bloquea una solicitud por otro canal.
- Consultar el estado no elimina XML/PDF ni la validación SAT ya obtenida. Un PDF
  aislado, una página de éxito o un XML ajeno no se presentan como CFDI validado.
- Los datos privados nunca vuelven a las herramientas de navegación pública o a
  la IA. El nuevo canal abre la red solo durante el paso confirmado, conserva
  transporte con IP pública fijada y TLS, bloquea medición y limita destinos a los
  mostrados. Después vuelve a cerrar la red. Las redirecciones a otro proveedor
  no reciben datos sin una revisión adicional.

Los recortes y borradores se guardan cifrados. Eliminar una solicitud o perfil
elimina también sus nuevas revisiones y recortes; no cancela una factura emitida.
No cambian configuración, claves, esquema SQL ni dependencias.

## Actualización

Actualizar ambos servicios y conservar sus volúmenes:

```bash
(
  set -e
  cd /opt/lilyapp-agent
  git pull --ff-only
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml build agent browser
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml up -d --no-deps --wait --wait-timeout 180 agent browser
  curl --fail --silent --show-error --max-time 20 https://agente.lilyapp.mx/health
)
```

En Telegram: `/estado` → **Continuar facturación**. No hace falta volver a subir
la CSF. Un llenado aislado previo se reconstruye en una sesión limpia antes de
presentar el envío real; jamás se reutiliza un control con `submit` deshabilitado.

## Verificación

Suite completa local: **753 pruebas** (387,39 s). Después del análisis final,
**77 regresiones** (102,34 s) cubren preparación, flujo de Telegram, llenado y
**21 casos nuevos de envío real**. Usan Chromium, el webhook y el almacenamiento
reales con comercios, OpenAI, correo y SAT simulados. No se emitieron facturas
reales durante estas pruebas. Incluyen dos formularios distintos, campos nuevos,
POST HTML/JSON, permisos, recortes ilegibles, cambio de perfil, vencimiento,
pérdida de respuesta, doble clic, CFDI ajeno y bloqueo de filtración a un CDN.
