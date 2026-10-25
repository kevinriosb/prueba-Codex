from dataclasses import dataclass
from typing import Callable, Literal

from agents import FunctionTool, RunContextWrapper, function_tool

from .calculator import CalculationError, calculate
from .clock import server_time


@dataclass(frozen=True)
class ToolSpec:
    handler: Callable
    effect: Literal['read', 'write'] = 'read'


def build_tools(settings, extras: tuple[ToolSpec, ...] = ()) -> list[FunctionTool]:
    @function_tool(failure_error_function=lambda _ctx, _error: "La herramienta rechazó los datos o no pudo completar la operación.", timeout=settings.tool_timeout_seconds)
    async def calculator(expression: str) -> dict:
        """Calcula una expresión con números, paréntesis y los operadores + - * /.

        Args:
            expression: Operación numérica con punto decimal. Máximo 200 caracteres.
        """
        try:
            return {"ok": True, "value": calculate(expression)}
        except CalculationError as error:
            return {"ok": False, "error": str(error)}

    @function_tool(timeout=settings.tool_timeout_seconds)
    async def current_server_time() -> dict:
        """Consulta la fecha y hora actual del servidor, indicando su zona horaria."""
        return server_time(settings.server_timezone)

    tools = [calculator, current_server_time]
    for spec in extras:
        if spec.effect not in {'read', 'write'}:
            raise ValueError('Tipo de efecto no permitido')
        # La política se impone en el servidor, no en el prompt ni en el navegador.
        tools.append(function_tool(spec.handler, needs_approval=spec.effect == 'write',
            timeout=settings.tool_timeout_seconds, timeout_behavior='raise_exception',
            failure_error_function=lambda _ctx, _error: 'La herramienta no pudo completar la operación.'))
    if len({tool.name for tool in tools}) != len(tools):
        raise ValueError('Los nombres de herramientas deben ser únicos')
    return tools
