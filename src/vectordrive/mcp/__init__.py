"""VectorDrive's read-only MCP server (M11): exposes search_drive,
read_document and index_status over stdio for Claude Desktop.

Deliberately does not import anything from vectordrive.pipeline.extract,
vectordrive.pipeline.ocr or vectordrive.pipeline.chunk — this package
structurally cannot index, rebuild, verify, prune, delete or modify
documents, because it never imports the code that does any of that
(requirement 3). It only reads already-indexed data through
vectordrive.mcp.readonly_db's read-only connection.
"""
