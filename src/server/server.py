

from pathlib import Path

from mcp.server import MCPServer


from scripts.sql_search import deepsearchsql


mcp= MCPServer("ezsql")


@mcp.tool()

# finds relevant sql files, or files that mention SQL/SQL terms. 



def find_relevant_files()->dict[str,list[str]]:
    root=Path().cwd()
    
    """ Use this to quickly find a list of important files to search in the directory. It returns a hashmap (Dictionary), where the  
    key is the folder/subfolder the relevant files are in, and the values are the list of filenames to search in those directories. This allows you to find relevant files quickly, and form a better plan. 
    Look at these files, and use them as a basis on which to create a plan. Cache the dictionary. 
    """
    return deepsearchsql(root)
    
    
    
    










