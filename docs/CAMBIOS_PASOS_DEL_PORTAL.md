# Ciclo general de pasos del portal

Fecha: 2026-10-31. Rama: `pasos-del-portal`. Base: `origin/main` en
`d35f65f` (PR #5). No se modificó ni fusionó `main`. No hay despliegue,
PR ni envíos a portales reales en este trabajo.

## Qué se pudo demostrar

Se reprodujo un caso que deja al bot en `unknown` aunque el siguiente
formulario ya esté visible: el envío devuelve el formulario nuevo y un
JavaScript auxiliar responde 404. La prueba
`test_new_form_is_not_unknown_when_an_auxiliary_response_fails` falló con
el código de la base: esperaba `review`, recibió `unknown`; la observación
ya contenía los tres campos del segundo formulario. Tras corregirlo pasa.
Esto demuestra un defecto concreto, pero **no demuestra que ese 404 fuese
la causa exacta de la ejecución real**: no se dispone de su respuesta ni
de su DOM original. No se probó el portal real.

Código causante en la base `d35f65f`:

- `backend/browser_worker/submission.py:117`: cualquier respuesta HTTP
  con estado >=400, incluso un recurso auxiliar, activaba `transport_error`.
  El `except` de `transaction_route` hacía lo mismo con excepciones.
- En ese archivo, `transaction_send:229–235` comprobaba ese error antes
  del cambio de campos. Además, si no había campos y el botón anterior
  se consideraba de emisión, caía en `unknown`; no existía un estado
  propio para las páginas intermedias de navegación. Su `except:239`
  conservaba el resultado incierto tras una excepción posterior al envío.
- `backend/app/billing/portal_submission.py:136–141`, método `status`:
  ante cualquier `AppError` devolvía la transacción sin registrar el motivo.
- `backend/app/billing/telegram_submission.py:71–75`: cada consulta volvía
  a llamar `show`, produciendo otra tarjeta completa idéntica.
- `backend/browser_worker/engine.py`, métodos `transaction_send` y
  `transaction_status`: delegan en `submission.py`; el worker decide el
  resultado, `PortalSubmission.accept` lo persiste y Telegram lo presenta.

La observación anterior ya reconocía algunos cambios de formulario: el
problema no era simplemente que estuviera prohibido tener dos pasos.

## Qué cambió

El número y el orden de campos se obtienen de cada pantalla, no de una
receta por comercio. El worker distingue formulario nuevo, navegación,
archivos, validación e incertidumbre. Compara etiquetas, tipos, obligatoriedad,
opciones y restricciones, sin utilizar los valores escritos para decidir
si cambió la estructura. Un fallo auxiliar no equivale por sí solo a un
envío perdido; un fallo de la petición principal o una respuesta aún en
curso siguen siendo inciertos. No se repite el clic para averiguarlo.

Cada formulario tiene dos autorizaciones distintas, de un solo uso:

1. Se observa sin escribir y se preparan los valores propuestos. Solo se
   relee la foto para los campos del ticket pedidos en esa pantalla que
   aún faltan. Los datos fiscales que falten se piden explícitamente.
2. **Autorizar llenado** escribe con el contexto del navegador sin red.
   No envía el formulario. Si el llenado revela campos u opciones nuevos,
   se vuelve a revisar y a pedir los datos que falten antes de continuar.
3. **Confirmar envío** autoriza únicamente el botón y los valores revisados
   de ese paso. No vuelve a rellenar ni autoriza el siguiente formulario.
   Si cambian sesión, alcance, perfil, valores o estructura, se rechaza.
4. Se observa el resultado y comienza otra revisión, o se termina/solicita
   intervención según la evidencia. Los errores de validación se muestran
   sin reintentar automáticamente.

No se infieren nombres, apellidos, tipo de persona ni correo. Se reconocen
las etiquetas de esos campos, pero si el perfil no contiene un valor
confirmado, debe proporcionarlo la persona. El correo se valida antes de
habilitar el llenado. Los campos fiscales no se extraen de una foto del
ticket. También se reconoce forma de pago como dato del ticket; puede
pedirse en cualquier formulario posterior.
El lector público de HTML comparte esas etiquetas y sus nombres de
presentación; una regresión adicional comprueba que todos se pueden
interpretar sin un error de clave ausente.

Las pantallas sin campos y con controles de avance tienen estado
`navigation`, no `unknown`. Se muestran los datos aprobados previamente
que el portal podría reenviar. **Dentro de una sesión que ya recibió datos
fiscales, también los enlaces GET requieren autorización**: no se puede
demostrar desde el DOM que el servidor no reutilizará esos datos. Se usan
los mismos destinos permitidos; no se concede ningún dominio nuevo.
La ruta pública anterior al llenado conserva sus filtros, límite de siete
acciones y plazo de 22 segundos. No se extiende ese plazo a toda la
conversación humana ni se abre una ruta automática privada con datos.

`BILLING_PORTAL_MAX_STEPS` limita los avances autorizados por intento
(predeterminado 8, configurable entre 1 y 32). Cuenta también navegación
privada; no cuenta lecturas ni la autorización de llenado. Al alcanzar el
límite sin terminar se detiene con mensaje explícito. No hay un número
fijo de formularios ni una posición reservada para datos fiscales/ticket.
El worker conserva su plazo de 18 segundos por operación y los límites
de tamaño y peticiones existentes.

El XML/PDF se recupera mediante el transporte existente, se comprueba la
integridad y se valida el XML contra el perfil/ticket. Los botones de
descarga y de autorización SAT siguen disponibles. No se consulta SAT
automáticamente. Si solo hay constancia de solicitud, no se inventa una
factura validada.

## Salidas para incertidumbre real

- Consultar vuelve a leer el DOM en modo offline si la sesión todavía
  existe; no escribe, no hace clic y no repite la petición. Puede recuperar
  un formulario ya recibido tras perderse la respuesta entre servicios.
- Si vence la sesión, se muestra esa causa. Cuando nada cambia, Telegram
  responde «No hay cambios» y conserva como referencia la tarjeta anterior,
  sin agregar otra tarjeta completa idéntica.
- **Reintentar desde el inicio** primero advierte del posible duplicado y
  exige confirmar que no se emitió. Solo entonces crea una sesión nueva,
  con permisos ya vigentes y sin reutilizar checkpoints privados. Cada
  llenado y envío vuelve a necesitar sus dos autorizaciones.
- **Revisar mi correo** indica revisar personalmente bandeja y spam. No
  accede al buzón, no envía correo y no afirma haberlo consultado.
- **Marcar como resuelto manualmente** cierra localmente esa incertidumbre.
  No afirma emisión, no fabrica XML ni marca como validado ante el SAT.

Los nuevos diagnósticos de resultado usan códigos constantes con el
logger existente; no agregan valores fiscales, texto de páginas ni URLs.

## Reproducciones y regresiones

Todas usan páginas inventadas, `prepared()`, `RealBrowser`, Chromium y
el transporte simulado de las pruebas de preparación/envío. No usan
llaves de servicios ni portales externos. Los nombres de controles no
activan una receta nueva: la misma implementación procesa controles DOM
y campos observados en todos los casos.

| Caso | Resultado | Estado esperado y evidencia |
| --- | --- | --- |
| Un formulario | Pasa | `submitted`; dos autorizaciones distintas, XML/PDF y XML cotejado |
| Dos formularios | Pasa | `review` tras el primero; datos fiscales y hora aparecen después |
| Tres formularios | Pasa | Correo al inicio; ticket, pago y caja en un paso posterior |
| Cuatro formularios | Pasa | Perfil y ticket alternan; solo se leen campos nuevos pedidos |
| Formulario → enlace GET → formulario | Pasa | `navigation`, autorización propia, nuevo `review`, archivos finales |
| Formulario → control POST sin campos visibles → formulario | Pasa | `navigation`; ningún envío implícito de datos retenidos |
| Recurso auxiliar 404 con formulario nuevo | Pasa | `review/new_form`, no falsa incertidumbre |
| Correo ausente o inválido | Pasa | Pide valor explícito; sin lectura de correo desde el ticket ni envío |
| Campos revelados durante el llenado | Pasa | `correction`; nueva revisión y lectura dirigida antes del envío |
| Validación rechazada por el portal | Pasa | `correction`; mensaje visible, sin reintento automático |
| Formulario idéntico sin evidencia de resultado | Pasa | `unknown/unclassified`; nunca presume éxito ni repite |
| Respuesta perdida entre servicios con DOM disponible | Pasa | Consulta recupera el formulario; cero escrituras/POST adicionales |
| Respuesta perdida y sesión vencida | Pasa | Tres salidas explícitas; reinicio requiere confirmación y respeta permisos |
| Límite configurado en dos avances | Pasa | `step_limit`; no hay tercer envío |
| Envío sin autorizar llenado o con datos cambiados | Pasa | Rechazado sin petición al portal |
| Tarjeta demasiado larga | Pasa | Sin botones de llenado/envío si no pueden mostrarse todos los valores |

Las pruebas verifican que observar no llena, que llenar no hace peticiones,
que repetir cualquiera de los botones no repite el POST, y que la consulta
SAT solo ocurre después de pulsar su autorización. La lectura dirigida se
verifica por el conjunto exacto de campos pedidos y la ausencia de lecturas
duplicadas, no solo por que la factura final exista.

En la ejecución completa de 938 pruebas, los escenarios de 1/2/3/4
formularios tardaron 2,031 / 2,990 / 3,478 / 4,218 segundos respectivamente.
Los dos escenarios de formulario → navegación → formulario tardaron
3,510 y 3,829 segundos. Son tiempos locales del caso completo, con modelo,
transporte y pulsaciones humanas simulados: **no estiman tiempos reales
de un portal ni la espera de una persona**. Cada formulario usa dos
autorizaciones; la pantalla privada intermedia agrega otras dos y un
avance al contador. No hay un salto de página supuesto por posición.

Se adaptaron pruebas antiguas al protocolo de dos autorizaciones: antes
llamaban una operación que llenaba y enviaba junta. Ahora autorizan llenado
y después envío, conservando sus aserciones de destinos, integridad,
aislamiento e idempotencia. No se quitaron, omitieron ni debilitaron pruebas
intermitentes. El adaptador `RealBrowser` traduce los errores del worker
a `AppError`, igual que el cliente HTTP real.

Comandos de reproducción desde la raíz, con Chromium instalado:

```bash
UV_CACHE_DIR=/workspace/.cache/uv uv run --frozen --group browser pytest -q tests/test_portal_steps_cycle.py tests/test_portal_submission.py
UV_CACHE_DIR=/workspace/.cache/uv uv run --frozen --group browser pytest -q --junitxml=.local/portal-cycle-final-all.xml
git diff --exit-code origin/main -- backend/app/billing/portal_permissions.py backend/browser_worker/policy.py backend/app/billing/store.py
```

Validación: el conjunto focal final dio **40 aprobadas, cero fallos**
(81,19 s). Dos ejecuciones completas anteriores al último control de campos
dinámicos dieron **937 aprobadas**, sin fallos. La siguiente dio **938
aprobadas**, cero fallos, errores u omisiones (450,61 s). Después se
completaron las etiquetas compartidas y la lectura dirigida al reiniciar;
la **suite completa final dio 939 aprobadas, cero fallos, cero errores y
cero omisiones**, en 446,91 segundos (7 min 26 s). Incluye
`tests/test_no_merchant_specific_routing.py` y las pruebas intermitentes
existentes; ninguna falló en esa ejecución. El resultado JUnit local queda
en `.local/portal-cycle-final-all.xml` (artefacto local, no versionado).

## Sin cambios y pendiente

No se cambiaron permisos, `policy.py`, `store.py`, cifrado, retención,
aprobaciones SAT ni reglas específicas por comercio. No hay reintentos
automáticos de envíos. El llenado aislado anterior sigue separado de las
transacciones del portal.

Quedan pendientes la integración/despliegue y la comprobación humana del
portal real. Agente y worker deben actualizarse juntos: el protocolo ahora
distingue llenar y enviar. Las sesiones privadas antiguas no se convierten
en aprobaciones nuevas. Los CAPTCHA, controles no admitidos y formularios
que excedan los límites de seguridad requieren ayuda; no se eluden.
Un formulario indistinguible del anterior sin confirmación ni archivos sigue
siendo incierto deliberadamente. No se promete resolver cualquier widget
desconocido ni recuperar la respuesta de una sesión ya destruida.
