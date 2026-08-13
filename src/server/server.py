

from pathlib import Path

from mcp.server import MCPServer


from scripts.sql_search import deepsearchsql


mcp= MCPServer("ezsql")


@mcp.tool()

# finds relevant sql files, or files that mention SQL/SQL terms. 



def find_relevant_files()->dict[str,list[str]]:
    root=Path().cwd()
    return deepsearchsql(root)
    
    
    
    










