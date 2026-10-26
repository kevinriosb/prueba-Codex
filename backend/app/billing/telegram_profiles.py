"""Alta y edición del perfil por botones, sin generación de IA ni acceso web."""
import re
import time
import unicodedata

from ..errors import AppError
from .models import CFDI_USES, validate_fiscal
from .providers import normalized_email


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.casefold())
        if unicodedata.category(c) != 'Mn').strip(' .!¡?¿')


class TelegramProfiles:
    def __init__(self, bridge):
        self.bridge, self.billing = bridge, bridge.billing

    async def send(self, chat_id, text, buttons=None):
        await self.bridge.client.send(chat_id, text, buttons)

    def current_draft(self, owner, identifier=None):
        state = self.bridge._state(owner)
        if not state.get('draft_id') or (identifier and state['draft_id'] != identifier):
            raise AppError(409, 'profile_step', 'Ese paso venció. Escribe /perfil.')
        return state, self.billing.store.get('profile_draft', owner, state['draft_id'])

    async def resume(self, owner, chat_id, *, edit=False):
        state = self.bridge._state(owner)
        draft, csf = self.bridge._profile_setup(owner, state)
        if draft:
            await self.show_draft(owner, chat_id, draft['id'])
            return
        if csf:
            if csf.get('extracted') is not None:
                draft = await self.billing.process_fiscal_certificate(owner, csf['id'],
                    consent=False, replace_profile_id=csf.get('profile_id'))
                await self.show_draft(owner, chat_id, draft['id'])
            else:
                await self.send(chat_id, 'Analizar la CSF la comparte con OpenAI y consume tokens. No se envía a comercios.',
                    [('Autorizar análisis', 'an:' + csf['id'])])
            return
        profiles = self.billing.store.list('profile', owner)
        profile = next((item for item in profiles if item['id'] == state.get('active_profile')), None)
        if not profile and len(profiles) == 1:
            profile = profiles[0]
        if profile:
            if edit:
                await self.edit(owner, chat_id, profile['id'])
            else:
                await self.send(chat_id, 'Perfil listo. Envía tu ticket.',
                    [('Ver o editar perfil', 'pe:' + profile['id'])])
        elif profiles:
            await self.send(chat_id, 'Elige tu perfil.',
                [(item['name'][:55], 'pp:' + item['id']) for item in profiles])
        else:
            await self.send(chat_id, 'Envía tu CSF una vez para crear tu perfil.')

    async def show_draft(self, owner, chat_id, identifier):
        draft = self.billing.store.get('profile_draft', owner, identifier)
        state = self.bridge._state(owner)
        # Versiones previas guardaban la elección de uso solo en el estado del chat.
        if state.get('draft_id') == identifier and not draft.get('cfdi_use') and state.get('cfdi_use') in CFDI_USES:
            draft['cfdi_use'] = state['cfdi_use']
            draft = self.billing.store.update('profile_draft', owner, identifier, draft, version=draft['_version'])
        state.update(draft_id=identifier, cfdi_use=draft.get('cfdi_use'), profile_input=None)
        self.bridge._save_state(owner, state)
        if not validate_fiscal(draft):
            await self.send(chat_id, 'CSF ilegible. Envía una copia clara con /csf.')
        elif not draft.get('cfdi_use'):
            await self.uses(owner, chat_id, identifier)
        elif not draft.get('email'):
            await self.ask_email(owner, chat_id, identifier)
        else:
            await self.review(owner, chat_id, identifier)

    async def uses(self, owner, chat_id, identifier):
        self.current_draft(owner, identifier)
        await self.send(chat_id, 'Elige tu uso de CFDI.',
            [(code + ' · ' + label[:50], 'cu:' + identifier + ':' + code) for code, label in CFDI_USES.items()])

    async def ask_email(self, owner, chat_id, identifier):
        state, _ = self.current_draft(owner, identifier)
        state['profile_input'] = 'email'
        self.bridge._save_state(owner, state)
        await self.send(chat_id, 'Escribe tu correo para recibir facturas.')

    async def choose_use(self, owner, chat_id, identifier, code):
        state, draft = self.current_draft(owner, identifier)
        if code not in CFDI_USES:
            raise AppError(422, 'cfdi_use', 'Elige un uso válido de CFDI.')
        draft['cfdi_use'] = code
        self.billing.store.update('profile_draft', owner, identifier, draft, version=draft['_version'])
        state['cfdi_use'] = code
        self.bridge._save_state(owner, state)
        await self.show_draft(owner, chat_id, identifier)

    async def review(self, owner, chat_id, identifier):
        _, draft = self.current_draft(owner, identifier)
        if not validate_fiscal(draft) or not draft.get('cfdi_use') or not draft.get('email'):
            await self.show_draft(owner, chat_id, identifier)
            return
        lookup = self.billing.store.digest(f'profile-review:{identifier}:{draft["_version"]}')
        approval = self.billing.store.find('profile_review', owner, lookup)
        if not approval:
            approval = self.billing.store.add('profile_review', owner,
                {'draft_id': identifier, 'draft_version': draft['_version']}, lookup=lookup, state='pending',
                expires=min(draft['_expires'], time.time() + 900))
        summary = 'Revisa y confirma aquí:\n' + '\n'.join(f'{label}: {draft[key]}' for key, label in
            [('rfc', 'RFC'), ('name', 'Nombre'), ('postal_code', 'CP fiscal'), ('fiscal_regime', 'Régimen'),
             ('cfdi_use', 'Uso CFDI'), ('email', 'Correo')])
        if draft.get('replace_profile_id'):
            summary += '\nSe actualizará tu perfil al confirmar.'
        await self.send(chat_id, summary, [('Confirmar y guardar', 'pf:' + approval['id']),
            ('Cambiar correo', 'pm:' + identifier), ('Cambiar uso CFDI', 'pu:' + identifier),
            ('Descartar cambios', 'pd:' + identifier)])

    async def confirm(self, owner, chat_id, identifier):
        try:
            approval = self.billing.store.get('profile_review', owner, identifier)
        except AppError as error:
            if error.status != 404:
                raise
            # Botones antiguos apuntaban al borrador sin versión: pedir revisión nueva.
            self.billing.store.get('profile_draft', owner, identifier)
            await self.show_draft(owner, chat_id, identifier)
            return
        _, draft = self.current_draft(owner, approval['draft_id'])
        if draft['_version'] != approval['draft_version']:
            raise AppError(409, 'profile_changed', 'Los datos cambiaron. Escribe /perfil y revisa.')
        self.billing.store.claim('profile_review', owner, identifier)
        profile = self.billing.confirm_fiscal_profile(owner, draft['id'], confirmed=True)
        state = self.bridge._state(owner)
        state.update(active_profile=profile['id'], draft_id=None, cfdi_use=None, profile_input=None,
            csf_document_id=None, replace_profile_id=None)
        self.bridge._save_state(owner, state)
        await self.send(chat_id, 'Perfil guardado. Envía tu ticket.')
        await self.bridge._pending_tickets(owner, chat_id)

    async def edit(self, owner, chat_id, identifier):
        profile = self.billing.store.get('profile', owner, identifier)
        state = self.bridge._state(owner)
        state['active_profile'] = identifier
        draft, _ = self.bridge._profile_setup(owner, state)
        if not draft or draft.get('replace_profile_id') != identifier:
            draft = self.billing.draft_profile_update(owner, identifier)
        self.bridge._save_state(owner, state)
        await self.show_draft(owner, chat_id, draft['id'])
        await self.send(chat_id, 'Para cambiar datos fiscales, actualiza tu CSF.',
            [('Actualizar CSF', 'px:' + profile['id'])])

    async def callback(self, owner, chat_id, prefix, identifier, code=None):
        if prefix == 'cu':
            await self.choose_use(owner, chat_id, identifier, code)
        elif prefix == 'pe':
            await self.edit(owner, chat_id, identifier)
        elif prefix == 'pm':
            await self.ask_email(owner, chat_id, identifier)
        elif prefix == 'pu':
            await self.uses(owner, chat_id, identifier)
        elif prefix == 'pp':
            self.billing.store.get('profile', owner, identifier)
            state = self.bridge._state(owner)
            state['active_profile'] = identifier
            self.bridge._save_state(owner, state)
            await self.resume(owner, chat_id)
            await self.bridge._pending_tickets(owner, chat_id)
        elif prefix == 'px':
            self.billing.store.get('profile', owner, identifier)
            state = self.bridge._state(owner)
            state['replace_profile_id'] = identifier
            self.bridge._save_state(owner, state)
            await self.send(chat_id, 'Envía la CSF actualizada. Luego la confirmarás.')

    async def text(self, owner, chat_id, text):
        plain = normalized(text)
        if plain in {'/perfil', 'tienes los datos para facturar', 'tienes datos para facturar',
                'ya tienes mis datos', 'ya tienes mis datos para facturar', 'completar perfil',
                'completar mi perfil', 'quiero completar mi perfil', 'confirmar perfil',
                'confirmar mi perfil', 'quiero confirmar mi perfil', 'mis datos fiscales',
                'tengo perfil fiscal', 'mi perfil', 'ver mi perfil'}:
            await self.resume(owner, chat_id)
            return True
        if plain in {'actualizar perfil', 'actualizar mi perfil', 'cambiar mis datos',
                'actualizar mis datos', 'cambiar mi correo', 'quiero cambiar mi correo',
                'quiero actualizar mi perfil'}:
            await self.resume(owner, chat_id, edit=True)
            return True
        state = self.bridge._state(owner)
        draft, csf = self.bridge._profile_setup(owner, state)
        if plain in {'si', 'continuar', 'listo'} and (draft or csf):
            # «Sí» solo abre el paso pendiente. No concede consentimiento ni confirma.
            await self.resume(owner, chat_id)
            return True
        code_match = re.fullmatch(r'(?:(?:uso(?: de)? cfdi|uso)\s*:?\s*)?([a-z]{1,2}\d{2})', plain)
        email_match = re.fullmatch(r'(?:(?:mi correo(?: es)?|correo(?: es)?)\s*:?\s*)?([^\s@]+@[^\s@]+)', text, re.I)
        if not (code_match or email_match):
            return False
        profiles = self.billing.store.list('profile', owner)
        if not draft and not csf and not profiles:
            return False
        if not draft:
            profile = next((item for item in profiles if item['id'] == state.get('active_profile')), None)
            if not profile and len(profiles) == 1:
                profile = profiles[0]
            if profile:
                draft = self.billing.draft_profile_update(owner, profile['id'])
            elif csf and csf.get('extracted') is not None:
                draft = await self.billing.process_fiscal_certificate(owner, csf['id'],
                    consent=False, replace_profile_id=csf.get('profile_id'))
            if not draft:
                await self.resume(owner, chat_id, edit=bool(profiles))
                return True
            state['draft_id'] = draft['id']
            self.bridge._save_state(owner, state)
        if code_match:
            await self.choose_use(owner, chat_id, draft['id'], code_match[1].upper())
        else:
            email = normalized_email(email_match[1])
            draft['email'] = email
            self.billing.store.update('profile_draft', owner, draft['id'], draft, version=draft['_version'])
            await self.show_draft(owner, chat_id, draft['id'])
        return True
