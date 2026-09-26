from enum import StrEnum


class EntityStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class GeometryType(StrEnum):
    NONE = "none"
    POINT = "point"
    LINE_STRING = "lineString"
    POLYGON = "polygon"


class MapGeometryType(StrEnum):
    POINT = "point"
    LINE_STRING = "lineString"
    POLYGON = "polygon"


class FieldType(StrEnum):
    STRING = "string"
    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    ADDRESS = "address"
    ENUM = "enum"
    REFERENCE = "reference"
    FILE = "file"


class MapRuleOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "notEquals"
    CONTAINS = "contains"
    FILLED = "filled"
    EMPTY = "empty"
    BEFORE = "before"
    AFTER = "after"
