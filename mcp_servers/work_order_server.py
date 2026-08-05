from mcp.server.fastmcp import FastMCP

mcp = FastMCP()

@mcp.tool()
def create_campus_work_order(location: str, problem_description: str, severity: str = "中", report_person: str = "匿名") -> str:
    return '''{"status":"ok"}'''

if __name__ == "__main__":
    mcp.run()