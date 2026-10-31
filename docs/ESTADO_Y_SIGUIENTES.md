# Estado del proyecto y siguientes sesiones

Actualizado: 2026-10-30. Base: `main` en `8b103b4` (PR #2, sesión 3A).

## Hecho
- Sesión 3A: elección del portal correcto con contexto, comprobación de que el host resuelve, respaldo (otras direcciones impresas, búsqueda del sitio oficial, pedir la web al usuario), ruta adaptable hasta el formulario sin lógica por comercio.
- Permisos: documentos e iframes de dominios nuevos requieren permiso humano; recursos auxiliares dentro de límites; al reanudar solo se abre el dominio de la página guardada.
- CI al fusionar: 896 de 897 pruebas; ver deuda 1.
- Desplegado en producción con `compose.hostinger.yaml` + `compose.browser.yaml` (agente y navegador sanos).

## Deuda prioritaria (hacer ANTES de funciones nuevas)
1. **Prueba intermitente**: `tests/test_ambiguous_receipt_photo.py::test_requested_field_photo_keeps_its_recorded_expectation_and_applies_without_a_ticket_choice[portal]`.
   - Mismo código: verde dos veces (#59 y su repetición), rojo en #57, #58 y en `main` tras la fusión.
   - En el fallo, el ticket queda sin datos leídos (importe, folio, tienda, fecha vacíos) y en `portal_stage='awaiting_form'`: la preparación (`prepared()`, botón `wa` con navegador real y portal simulado) no llegó al formulario a tiempo.
   - Tarea: hacer la prueba determinista (esperar a que el formulario esté listo y afirmarlo explícitamente), y confirmar que no es una carrera real del producto al seguir la ruta. No debilitar las aserciones. Hay un mensaje de diagnóstico (`DIAGNOSTICO2`) en la prueba que puede retirarse al cerrar esto.
   - También se vio flaky una vez `test_brand_selector_regressions::test_auxiliary_write_retry_storm_still_hits_page_request_limit`.

## Decisión de producto nueva (entra en S3)
- No conservar PDF/XML. Entregarlos por Telegram y por correo; el correo del perfil en el portal sirve de archivo (el portal suele enviar el CFDI).
- Borrar del servidor al confirmarse al menos un envío; límite máximo 24 horas. Si pasan 24 h sin envío confirmado, borrar y avisar que se recupera desde el portal (folio) o el SAT.
- El XML es el comprobante legal (conservar ~5 años en el correo del usuario). El envío por correo desde el agente con Gmail en modo de pruebas caduca a los 7 días: la ruta principal es el correo que envía el portal.

## Sesiones siguientes
- S3: retención y estado «completado» sin XML, capacidad, respaldos automáticos (+ decisión anterior).
- S4: calidad de foto antes de analizar, orden de resolución, confianza por campo.
- S5: separación y rotación de claves, RFC en chat libre, seguimiento de plazos, guardia SAT, quitar herramientas de navegador del chat general.
- S6: limpieza de documentación.
- S7: CI con análisis de estilo, auditoría de dependencias y construcción de imagen Docker.
- S8 (al final, solo si el agente ya factura bien en portales reales y se va a compartir): multiusuario y panel de administración.
  - Hoy el portal web (`frontend/`) es mínimo y de una sola persona (contraseña `APP_LOGIN_PASSWORD`); no hay usuarios ni roles.
  - Requisitos previos: aislamiento por usuario (identidad = ID de Telegram), límites y gasto de IA por usuario, separación y rotación de claves (S5), baja real (borrar perfil, CSF y tickets), aviso de privacidad y consentimiento (revisar con un abogado; LFPDPPP).
  - Orden: primero comandos de administrador en Telegram (`/usuarios`, `/baja`, `/uso`); después un panel web solo para el administrador, con segunda verificación.
  - Funciones deseadas: administrar perfiles, darlos de baja, ver la información de tickets del día y la CSF por perfil (retención máxima de un día para tickets).
  - Pendiente de definir por el dueño: cuántas personas lo usarán (familia vs. clientes cambia el alcance).

## Reglas de trabajo
- Parte siempre del último commit de `main` (`git pull`). No reutilizar ramas viejas.
- Un solo agente por rama a la vez.
- Sin lógica específica por comercio (lo vigila `tests/test_no_merchant_specific_routing.py`).
- Nunca pegar la clave de cifrado en el chat. Ningún envío real a un portal sin aprobación expresa del usuario.
