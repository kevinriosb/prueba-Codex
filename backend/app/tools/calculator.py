import ast
from decimal import Decimal, DecimalException, localcontext


class CalculationError(ValueError):
    pass


def calculate(expression: str) -> str:
    """Evalúa únicamente números, paréntesis y + - * /, sin ejecutar Python."""
    if not isinstance(expression, str) or not 1 <= len(expression) <= 200:
        raise CalculationError("La operación debe tener entre 1 y 200 caracteres.")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise CalculationError("Escribe una operación válida con punto decimal y +, -, * o /.") from None
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise CalculationError("La operación es demasiado compleja.")

    def walk(node, depth=0):
        if depth > 12:
            raise CalculationError("Demasiados paréntesis o términos.")
        if isinstance(node, ast.Expression):
            return walk(node.body, depth + 1)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            raw = ast.get_source_segment(expression.strip(), node)
            if not raw or len(raw) > 32 or any(char not in '0123456789.eE+-' for char in raw):
                raise CalculationError("Número no permitido.")
            value = Decimal(raw)
            if not value.is_finite() or abs(value) > Decimal('1e12') or value.as_tuple().exponent < -12:
                raise CalculationError("Número fuera de los límites permitidos.")
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = walk(node.operand, depth + 1)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = walk(node.left, depth + 1), walk(node.right, depth + 1)
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            else:
                value = left / right
            if not value.is_finite() or abs(value) > Decimal('1e12'):
                raise CalculationError("Resultado fuera de los límites permitidos.")
            return value
        raise CalculationError("Solo se permiten números, paréntesis y +, -, * o /.")

    try:
        with localcontext() as context:
            context.prec = 28
            result = walk(tree)
            return format(result, 'f').rstrip('0').rstrip('.') if '.' in format(result, 'f') else str(result)
    except DecimalException:
        raise CalculationError("La operación no es válida; comprueba los valores y las divisiones entre cero.") from None
