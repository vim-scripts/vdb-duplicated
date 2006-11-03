import math
import pty
import sys
import os
import shlex
import fcntl
import time
import re
import vim
import tty

VDB_DEBUG = False

tpipesplit = """
class TPipeSplit(object):
	def __init__(self, debugout, processout):
		self.debugout = debugout
		self.processout = processout
		#self.dump = open('vdb.debug', 'w')
	
	def write(self, buf):
		#self.dump.write('%r: %r\\n'%(os.path.basename(sys._getframe(1).f_code.co_filename), buf))
		#self.dump.flush()
		if os.path.basename(sys._getframe(1).f_code.co_filename) in ['cmd.py', 'pdb.py', 'bdb.py', '<stdin>']:
			self.debugout.write(buf)
		else:
			self.processout.write(buf)
"""

interface = [
	{
		"interfacename": "pdb",
		"exec": "/usr/bin/python -i -m pdb %(VDBSourceFile)s %(VDBArgs)s",
		"autostart": [
			"exec %r"%(tpipesplit), 
			"import sys", 
			"import os",
			"sys.stdout = TPipeSplit(sys.__stdout__, open('/tmp/vdbo-"+str(os.getpid())+"','w',0))"
		],
		"prompt": "(Pdb) ",
		"filetypecheck": "VDBSourceFile.endswith('.py')",
		"autoresponse": [
			("\(Pdb\) .*", ""),
			("^\s*> <string>\(1\)\?\(\)->None", ""),
			("^\s*-> .*", ""),
			("^\s*> <string>\((\d+)\).*", "self.writeline('next')"),
			("^\s*> (.*)\((\d+)\).*\(\)", "VDBShowExecution('%1', %2)"),
			("^\s*> (.*)\((\d+)\).*\(\)->\((.*)\)", "VDBShowExecution('%1', %2, result=%3)"),
			("^\s*--Return--", "if len(VDBStack) > 0: VDBStack.pop()"),
			("^\s*--Call--", "self.framecapture = True; self.writeline('next')"),
			("^\s*> <string>.*", "self.writeline('return')"),
			("^\s*Breakpoint (\d+) at (.*):(\d+)", "self.breaknum = %1"),
			("^\s*\*\*\* There are no breakpoints in .*", ""),
			("^\s*\*\*\* There is no breakpoint at .*:\d+", ""),
			("^\s*End of file", "self.breaknum = None"),
			("^\s*\*\*\* Blank or comment", "self.breaknum = None"),
			("^\s*Breakpoint (\d+) is now unconditional.", "self.breaknum = %1"),
			("^\s*The program finished and will be restarted", "self.autokill = True"),
			("^\s*SyntaxError: \('(.*)', \('(.*)', (\d+), \d+, '(.*)'\)\)", "self.state = ERROR; VDBShowExecution('%2', %3, error='%1')"),
			("^\s*(?:\*\*\*)* ?(.*Error:.*)", "self.state = ERROR; sys.stderr.write('%1\\n')")
		]
	}
]

VDBSession = None
VDBWatchWindow = None
VDBWatchBuffer = None
VDBOutputWindow = None
VDBOutputBuffer = None
VDBStackWindow = None
VDBConsoleWindow = None
VDBSourceWindow = None
VDBExecFilename = None
VDBSourceFile = None
VDBRuntimeArgStr = None
VDBBreakpoint = {}
VDBWatches = []
VDBStack = []
VDBErrorSign = None

READY = 0
ERROR = -1
CONSOLE = 1
INPUT = 2

def VDBSetDebugFile():
	name = VDBGetInput("Enter name of debug log file: ")
	if name is not None:
		debug = open(name,"w")
		debug.close()
		VDB_DEBUG = True

def debuglog(str, suppress = False):
	if VDB_DEBUG:
		debug = open("debug.out","a")
		if suppress:
			debug.write(str)
		else:
			debug.write(str+"\n")
		debug.close()

class TVDBSession(object):
	def __init__(self, interface, VDBSourceFile, VDBArgs):
		self.interface = interface
		self.debugqueue = []
		self.console = ['']
		self.autokill = False
		self.state = READY
		self.unmodifiablebuffers = []
		self.framecapture = False
		
		if (not os.path.exists("/tmp/vdbo-"+str(os.getpid()))):
			os.mkfifo("/tmp/vdbo-"+str(os.getpid()),0644)

		(pid, master) = pty.fork()
		if pid == 0:
			attrs = tty.tcgetattr(1)
			attrs[3] = attrs[3] & ~tty.ECHO
			attrs[0] = attrs[0] & tty.IGNBRK
			tty.tcsetattr(1, tty.TCSANOW, attrs)
			argv = shlex.split(self.interface["exec"] % {"VDBSourceFile": VDBSourceFile, "VDBArgs": VDBArgs})
			os.execv(argv[0], argv) 

		self.sendpipe = os.fdopen(master, "w", 0)
		self.receivepipe = os.fdopen(master, "r", 0)
		flags = fcntl.fcntl(self.receivepipe.fileno(), fcntl.F_GETFL, 0)
		flags = flags | os.O_NONBLOCK
		fcntl.fcntl(self.receivepipe.fileno(), fcntl.F_SETFL, flags)

		self.buffer = ""
		self.bufferwritepos = 0
		self.readlines()
	
		for asc in self.interface["autostart"]:
			self.writeline(asc)
			self.readlines()

		self.outputpipe = open("/tmp/vdbo-"+str(os.getpid()),"r",0)
		flags = fcntl.fcntl(self.outputpipe.fileno(), fcntl.F_GETFL, 0)
		flags = flags | os.O_NONBLOCK
		fcntl.fcntl(self.outputpipe.fileno(), fcntl.F_SETFL, flags)

	def __del__(self):
		try:
			self.outputpipe.close()
			os.remove("/tmp/vdbo-"+str(os.getpid()))
			self.receivepipe.close()
			self.sendpipe.close()
		except IOError:
			pass
	
	def _readline(self):
		try:
			char = self.receivepipe.read(1)
			while char != "\r":
				self.buffer += char
				char = self.receivepipe.read(1)
			self.receivepipe.read(1)
			line = self.buffer
			self.buffer = ""
			debuglog(line)
			self.console[len(self.console)-1] += line
			self.console.append('')
			return line
		except IOError:
			debuglog(self.buffer[self.bufferwritepos:],True)
			self.console[len(self.console)-1] += self.buffer[self.bufferwritepos:]
			if self.bufferwritepos < len(self.buffer):
				self.bufferwritepos += len(self.buffer)
			return None
	
	def readline(self):
		line = self._readline()
		if line is None:
			time.sleep(0.1)
			line = self._readline()
		return line
	
	def readlines(self):
		global VDBOutputWindow
		global VDBOutputBuffer

		ret = False
		line = self.readline()
		while line is not None:
			self.debugqueue.append(line)
			line = self.readline()
			ret = True
		if not self.buffer.startswith(self.interface["prompt"]):
			self.state = INPUT
			if VDBOutputWindow is None and VDBOutputBuffer is not None:
				vim.command("silent %inew %s"%(vim.current.window.height/2, "[Process Output]"))
				vim.command("setlocal buftype=nofile nowrap noautoindent nobuflisted tw=0 nomodifiable")
				vim.command("autocmd InsertLeave <buffer> setlocal nomodifiable")
				VDBOutputWindow = vim.current.window
				VDBOutputBuffer = vim.current.buffer
				vim.command("nmap <silent> <buffer> i :python VDBInputInsert()<CR>")
				vim.command("imap <silent> <buffer> <Bs> <C-\><C-O>:python VDBConsoleKeystroke(0)<CR>")
				vim.command("imap <silent> <buffer> <Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
				vim.command("imap <silent> <buffer> <C-Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
				vim.command("imap <silent> <buffer> <S-Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
				vim.command("imap <silent> <buffer> <Home> <C-\><C-O>:python VDBConsoleKeystroke(2)<CR>")
				vim.command("imap <silent> <buffer> <kHome> <C-\><C-O>:python VDBConsoleKeystroke(2)<CR>")
				vim.command("inoremap <buffer> <CR> <C-\><C-O>:python VDBConsoleKeystroke(-1)<CR>")
				for k in ["<Up>", "<Down>", "<S-Up>", "<S-Down>", "<S-Left>", "<S-Right>", "<C-Left>", "<C-Right>", "<C-Up>", "<C-Down>", "<PageUp>", "<PageDown>", "<kPageUp>", "<kPageDown>", "<kEnter>", "<C-w>"]:
					vim.command("imap <silent> <buffer> %s <C-\><C-O>:python VDBConsoleKeystroke(-2)<CR>"%(k))
				vim.command("autocmd BufLeave <buffer> python VDBOutputWindow = VDBWindowDeleted(VDBOutputWindow)")
				vim.command("autocmd BufDelete <buffer> python VDBOutputBuffer = VDBOutputWindow = None")
			if VDBOutputBuffer is not None:
				VDBFindWindow(VDBOutputWindow)
				vim.command("setlocal modifiable")
				vim.command("normal G")
				self.consoleprompt = VDBOutputBuffer[len(VDBOutputBuffer)-1]
				vim.command("startinsert!")
		else:
			if self.state == INPUT:
				self.state = READY
		return ret
	
	def write(self, str):
		self.buffer = ""
		self.bufferwritepos = 0
		self.sendpipe.write(str)
		self.console[len(self.console)-1] += str
		debuglog(str,True)
		time.sleep(0.01)

	def writeline(self, str):
		self.buffer = ""
		self.bufferwritepos = 0
		self.sendpipe.write(str+"\n")
		self.console[len(self.console)-1] += str
		self.console.append('')
		debuglog(str)
		time.sleep(0.01)

	def getoutput(self):
		try:
			outputlines = self.outputpipe.read()
			if outputlines == -1:
				return None
			else:
				return outputlines
		except IOError:
			return None
	
	def process(self, catch=False):
		global VDBOutputWindow
		global VDBOutputBuffer

		output = self.getoutput()
		if output is not None:
			if VDBOutputWindow is None:
				vim.command("silent %inew %s"%(vim.current.window.height/2, "[Process Output]"))
				vim.command("setlocal buftype=nofile nowrap noautoindent nobuflisted tw=0 nomodifiable")
				vim.command("autocmd InsertLeave <buffer> setlocal nomodifiable")
				VDBOutputWindow = vim.current.window
				VDBOutputBuffer = vim.current.buffer
				vim.command("nmap <silent> <buffer> i :python VDBInputInsert()<CR>")
				vim.command("imap <silent> <buffer> <Bs> <C-\><C-O>:python VDBConsoleKeystroke(0)<CR>")
				vim.command("imap <silent> <buffer> <Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
				vim.command("imap <silent> <buffer> <C-Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
				vim.command("imap <silent> <buffer> <S-Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
				vim.command("imap <silent> <buffer> <Home> <C-\><C-O>:python VDBConsoleKeystroke(2)<CR>")
				vim.command("imap <silent> <buffer> <kHome> <C-\><C-O>:python VDBConsoleKeystroke(2)<CR>")
				vim.command("inoremap <buffer> <CR> <C-\><C-O>:python VDBConsoleKeystroke(-1)<CR>")
				for k in ["<Up>", "<Down>", "<S-Up>", "<S-Down>", "<S-Left>", "<S-Right>", "<C-Left>", "<C-Right>", "<C-Up>", "<C-Down>", "<PageUp>", "<PageDown>", "<kPageUp>", "<kPageDown>", "<kEnter>", "<C-w>"]:
					vim.command("imap <silent> <buffer> %s <C-\><C-O>:python VDBConsoleKeystroke(-2)<CR>"%(k))
				vim.command("autocmd BufLeave <buffer> python VDBOutputWindow = VDBWindowDeleted(VDBOutputWindow)")
				vim.command("autocmd BufDelete <buffer> python VDBOutputBuffer = VDBOutputWindow = None")

			VDBFindWindow(VDBOutputWindow)
			vim.command("setlocal modifiable")
			for c in output:
				if c == "\n":
					VDBOutputBuffer.append('')
					vim.command("normal G$")
				else:
					VDBOutputBuffer[len(VDBOutputBuffer)-1] += c
			vim.command("setlocal nomodifiable")
		
		self.readlines()
		if self.autokill:
			return

		while len(self.debugqueue) > 0:
			response = self.debugqueue[0]
			if catch:
				if response != "":
					self.catchline = response
				del self.debugqueue[0]
				return
			matched = False
			for pair in self.interface["autoresponse"]:
				m = re.match(pair[0],response)
				if m is not None:
					comm = pair[1]
					if comm != "":
						for i in range(len(m.groups())+1):
							grp = m.group(i).rstrip().replace("'","\\'").replace("\"","\\\"")
							comm = comm.replace("%%%i"%(i),grp)
						exec comm in globals(), locals()
						if self.autokill:
							return True
						self.readlines()
					matched = True
					break
			del self.debugqueue[0]

class TBreakpoint(object):
	def __init__(self, signnum, buffer, number):
		self.signnum = signnum
		self.buffer = buffer
		self.number = number
		self.condition = ""

def VDBGetInput(prompt="VDB>", default="", complete="file"):
	if complete is None:
		return vim.eval('input("%s","%s")'%(prompt, default))
	else:
		return vim.eval('input("%s","%s","%s")'%(prompt, default, complete))

def VDBFindWindow(win):
	curwin = vim.current.window
	while True:
		if vim.current.window == win:
			return True
			break
		vim.command("wincmd w")
		if vim.current.window == curwin:
			return False
			break

def VDBWindowDeleted(win):
	if 'deleted' in repr(win):
		return None
	else:
		return win
	
def VDBShowExecution(filename, lineno, result=None, error=None):
	global VDBExecFilename
	global VDBBreakpoint
	
	if VDBExecFilename is not None:
		vim.command("silent sign jump 65535 file=%s"%(VDBExecFilename))
		vim.command("sign unplace 65535 file=%s"%(VDBExecFilename))
		key = "%s:%i"%(vim.current.buffer.name, vim.current.window.cursor[0])
		if VDBBreakpoint.has_key(key):
			if VDBBreakpoint[key].condition == "":
				vim.command("sign place %i line=%i name=BreakPoint file=%s"%(VDBBreakpoint[key].signnum,vim.current.window.cursor[0],VDBBreakpoint[key].buffer))
			else:
				vim.command("sign place %i line=%i name=CondBreakPoint file=%s"%(VDBBreakpoint[key].signnum,vim.current.window.cursor[0],VDBBreakpoint[key].buffer))
	
	if not VDBFindWindow(VDBSourceWindow):
		raise AssertionError("Yikes! The source window has disappeared!")
		return
	if VDBSourceWindow.buffer.name != filename:
		vim.command("silent edit %s"%(filename))
	vim.command("setlocal nomodifiable")
	if vim.current.buffer.number not in VDBSession.unmodifiablebuffers:
		VDBSession.unmodifiablebuffers.append(vim.current.buffer.number)

	if VDBSession.state == READY:
		vim.command("sign place 65535 line=%i name=ExecutionLine file=%s"%(lineno,filename))
	else:
		vim.command("sign place 65535 line=%i name=ErrorLine file=%s"%(lineno,filename))

	VDBExecFilename = filename
	vim.command("silent! sign jump 65535 file=%s"%(filename))
	key = "%s:%i"%(vim.current.buffer.name, vim.current.window.cursor[0])
	if VDBBreakpoint.has_key(key):
		vim.command("sign unplace %i file=%s"%(VDBBreakpoint[key].signnum,VDBBreakpoint[key].buffer))
	vim.command("silent! foldopen")
	if VDBSession.framecapture:
		VDBSession.framecapture = False
		VDBStack.append((VDBExecFilename, vim.current.window.cursor[0], vim.current.line))
	if error is not None:
		sys.stderr.write("%r\n"%(error))

def VDBUpdateWatches():
	global VDBSession
	global VDBWatches
	global VDBWatchWindow
	global VDBWatchBuffer

	if VDBSession.state == INPUT:
		return
	if VDBStackWindow is not None:
		win = vim.current.window
		VDBFindWindow(VDBStackWindow)
		vim.command("setlocal modifiable")
		del vim.current.buffer[:]
		fw = 0
		lw = 0
		for frame in VDBStack:
			if len(frame[0]) > fw:
				fw = len(frame[0])
			if math.log(frame[1],10) > lw:
				lw = math.log(frame[1],10)
		for frame in VDBStack:
			line = "%%-%is %%%ii: %%s"%(fw+3, lw+1)
			vim.current.buffer.append(line%frame)
		vim.command("normal ggdd")
		vim.command("setlocal nomodifiable")
		VDBFindWindow(win)

	if len(VDBWatches) == 0:
		if VDBWatchWindow is not None:
			VDBFindWindow(VDBWatchWindow)
			vim.command("wincmd c")
	else:
		if (VDBWatchBuffer is None) or (VDBWatchWindow is None):
			win = vim.current.window
			vim.command("silent %inew %s"%(vim.current.window.height/5, "[Watches]"))
			vim.command("setlocal buftype=nofile nowrap noautoindent nobuflisted tw=0")
			VDBWatchWindow = vim.current.window
			VDBWatchBuffer = vim.current.buffer
			vim.command("autocmd BufLeave <buffer> python VDBWatchWindow = VDBWindowDeleted(VDBWatchWindow)")
			vim.command("autocmd BufDelete <buffer> python VDBWatchBuffer = VDBWatchWindow = None")
			VDBFindWindow(win)
		
		del VDBWatchBuffer[:]
		count = 0
		for w in VDBWatches:
			if VDBSession is not None:
				VDBSession.writeline("print %s"%(w))
				VDBSession.process(catch=True)
				VDBWatchBuffer[len(VDBWatchBuffer)-1] = "%s: %s"%(w, VDBSession.catchline)
				count +=1
				if count < len(VDBWatches):
					VDBWatchBuffer.append("")
			else:
				VDBWatchBuffer[len(VDBWatchBuffer)-1] = "%s: Debug session not in progress"%(w)
				count +=1
				if count < len(VDBWatches):
					VDBWatchBuffer.append("")
	
	if VDBConsoleWindow is not None and VDBWindowDeleted(VDBConsoleWindow) is not None:
		win = vim.current.window
		VDBShowConsole(True)
		VDBFindWindow(win)

def VDBInputInsert():
	if VDBSession.state == INPUT:
		vim.command("setlocal modifiable")
		vim.command("normal G")
		vim.command("startinsert!")

def VDBConsoleKeystroke(key):
	if key == -2:
		return
	if key == -1: #<CR> enter/return
		if vim.current.window != VDBOutputWindow:
			win = vim.current.window
		else:
			vim.command("setlocal modifiable")
			vim.current.buffer.append('')
			vim.command("setlocal nomodifiable")
			win = None
		VDBSession.writeline(vim.current.line[len(VDBSession.consoleprompt):])
		VDBSession.process()
		VDBUpdateWatches()
		if win is not None:
			VDBFindWindow(win)
			vim.command("setlocal modifiable")
			vim.current.buffer[:] = VDBSession.console
			vim.command("normal G")
			vim.command("startinsert!")
		else:
			vim.command("stopinsert!")
		return
	col = vim.current.window.cursor[1]
	if key == 0: #<Bs> backspace
		if col > len(VDBSession.consoleprompt):
			if col < len(vim.current.line):
				vim.command("normal h")
			else:
				vim.command("normal $")
			vim.command("setlocal modifiable")
			vim.current.line = vim.current.line[0:col-1]+vim.current.line[col:]
	if key == 1: #<Left> left cursor
		#vim.current.buffer.append("col %i, pr %i, line %i"%(col, len(VDBSession.consoleprompt), len(vim.current.line)))
		if col > len(VDBSession.consoleprompt):
			if col < len(vim.current.line):
				vim.command("normal h")
			else:
				vim.command("normal h")
				vim.command("normal l")
	if key == 2: #<Home> home
		while vim.current.window.cursor[1] > len(VDBSession.consoleprompt):
			vim.command("normal h")
	vim.command("setlocal modifiable")

def VDBShowConsole(suppressinsert = False):
	global VDBConsoleWindow
	if VDBSession is None:
		VDBInitSession()
		VDBSession.process()
	if VDBConsoleWindow is None:
		VDBFindWindow(VDBSourceWindow)
		vim.command("silent! 10new [VDB Console]")
		vim.command("setlocal buftype=nofile nowrap noautoindent nobuflisted bufhidden=delete tw=0")
		vim.command("autocmd InsertLeave <buffer> setlocal nomodifiable")
		VDBConsoleWindow = vim.current.window
		vim.command("nmap <buffer> i :setlocal modifiable<CR>gi")
		vim.command("imap <silent> <buffer> <Bs> <C-\><C-O>:python VDBConsoleKeystroke(0)<CR>")
		vim.command("imap <silent> <buffer> <Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
		vim.command("imap <silent> <buffer> <C-Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
		vim.command("imap <silent> <buffer> <S-Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
		vim.command("imap <silent> <buffer> <Home> <C-\><C-O>:python VDBConsoleKeystroke(2)<CR>")
		vim.command("imap <silent> <buffer> <kHome> <C-\><C-O>:python VDBConsoleKeystroke(2)<CR>")
		vim.command("inoremap <buffer> <CR> <C-\><C-O>:python VDBConsoleKeystroke(-1)<CR>")
		for k in ["<Up>", "<Down>", "<S-Up>", "<S-Down>", "<S-Left>", "<S-Right>", "<C-Left>", "<C-Right>", "<C-Up>", "<C-Down>", "<PageUp>", "<PageDown>", "<kPageUp>", "<kPageDown>", "<kEnter>", "<C-w>"]:
			vim.command("imap <silent> <buffer> %s <C-\><C-O>:python VDBConsoleKeystroke(-2)<CR>"%(k))
		vim.command("autocmd BufLeave <buffer> python VDBConsoleWindow = VDBWindowDeleted(VDBConsoleWindow)")
		vim.command("autocmd BufDelete <buffer> python VDBConsoleBuffer = VDBConsoleWindow = None")
	else:
		VDBFindWindow(VDBConsoleWindow)
	vim.command("setlocal modifiable")
	vim.current.buffer[:] = VDBSession.console
	VDBSession.consoleprompt = vim.current.buffer[len(vim.current.buffer)-1]
	vim.command("normal G$")
	if not suppressinsert:
		vim.command("startinsert!")

#returns True is there is a running session already, otherwise attempts to start a new session, then returns true
#returns false if a new session cannot be started
def VDBInitSession(ReInit = None):
	global VDBSession
	global VDBSourceFile
	global VDBRuntimeArgStr
	global VDBWatchWindow
	global VDBWatchBuffer
	global VDBOutputWindow
	global VDBOutputBuffer
	global VDBStackWindow
	global VDBSourceWindow

	if ReInit:
		VDBKill()
	if VDBSession is None:
		if (VDBSourceFile is None) or (ReInit):
			VDBSourceFile = VDBGetInput("Enter filename of source file to debug: ", vim.current.buffer.name)
			VDBRuntimeArgStr = VDBGetInput("Enter command line arguments: ", "")

		i = None
		for testi in interface:
			if eval(testi["filetypecheck"]):
				i = testi
				break
		if i is None:
			raise AssertionError("Cannot find appropriate interface for file %s"%(VDBSourceFile))
			return False
	
		try:
			vim.command("silent wincmd o")
		except VimError:
			print "Cannot start debugger whilst modified buffers are open"
			return False
		VDBWatchWindow = None
		VDBWatchBuffer = None
		VDBStackWindow = None
		
		VDBSession = TVDBSession(i,VDBSourceFile, VDBRuntimeArgStr)
		
		vim.command("silent edit %s"%(VDBSourceFile))
		VDBSourceWindow = vim.current.window
		vim.command("autocmd BufLeave <buffer> python if VDBWindowDeleted(VDBSourceWindow) is None: VDBKill()")
		
		vim.command("silent %inew %s"%(vim.current.window.height/5, "[Process Output]"))
		vim.command("setlocal buftype=nofile nowrap noautoindent nobuflisted tw=0")
		vim.command("autocmd InsertLeave <buffer> setlocal nomodifiable")
		VDBOutputWindow = vim.current.window
		VDBOutputBuffer = vim.current.buffer
		vim.command("nmap <silent> <buffer> i :python VDBInputInsert()<CR>")
		vim.command("imap <silent> <buffer> <Bs> <C-\><C-O>:python VDBConsoleKeystroke(0)<CR>")
		vim.command("imap <silent> <buffer> <Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
		vim.command("imap <silent> <buffer> <C-Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
		vim.command("imap <silent> <buffer> <S-Left> <C-\><C-O>:python VDBConsoleKeystroke(1)<CR>")
		vim.command("imap <silent> <buffer> <Home> <C-\><C-O>:python VDBConsoleKeystroke(2)<CR>")
		vim.command("imap <silent> <buffer> <kHome> <C-\><C-O>:python VDBConsoleKeystroke(2)<CR>")
		vim.command("inoremap <buffer> <CR> <C-\><C-O>:python VDBConsoleKeystroke(-1)<CR>")
		for k in ["<Up>", "<Down>", "<S-Up>", "<S-Down>", "<S-Left>", "<S-Right>", "<C-Left>", "<C-Right>", "<C-Up>", "<C-Down>", "<PageUp>", "<PageDown>", "<kPageUp>", "<kPageDown>", "<kEnter>", "<C-w>"]:
			vim.command("imap <silent> <buffer> %s <C-\><C-O>:python VDBConsoleKeystroke(-2)<CR>"%(k))
		vim.command("autocmd BufLeave <buffer> python VDBOutputWindow = VDBWindowDeleted(VDBOutputWindow)")
		vim.command("autocmd BufDelete <buffer> python VDBOutputBuffer = VDBOutputWindow = None")
		del VDBOutputBuffer[:]

		vim.command("wincmd w")
		vim.command("wincmd w")

		disable = []
		for key in VDBBreakpoint:
			VDBSession.writeline("break %s"%(key))
			VDBSession.process()
			if VDBSession.breaknum is None:
				vim.command("sign unplace %i file=%s"%(VDBBreakpoint[key].signnum,VDBBreakpoint[key].buffer))
				disable.append(key)
				print "Breakpoint at %s is invalid, deleting ..."%(key)
		for key in disable:
			del VDBBreakpoint[key]
			
		vim.command("highlight ExecutionLine term=bold ctermbg=DarkGreen ctermfg=White")
		vim.command("highlight ErrorLine term=inverse ctermbg=DarkRed ctermfg=Black")
		vim.command("highlight StackLine term=inverse ctermbg=DarkBlue ctermfg=Black")
		vim.command("highlight BreakPoint term=inverse ctermbg=DarkCyan ctermfg=Black")

		vim.command("sign define ExecutionLine text==> texthl=ExecutionLine linehl=ExecutionLine")
		vim.command("sign define ErrorLine text==> texthl=ErrorLine linehl=ErrorLine")
		vim.command("sign define StackLine text=<> texthl=StackLine linehl=StackLine")
		vim.command("sign define BreakPoint text=! texthl=BreakPoint linehl=BreakPoint")
		vim.command("sign define CondBreakPoint text=? texthl=BreakPoint linehl=BreakPoint")

		return True
	return True

def VDBShowStack():
	global VDBStackWindow

	if (VDBSession is not None) and (VDBStackWindow is None):
		win = vim.current.window
		vim.command("silent %inew %s"%(vim.current.window.height/5, "[Call Stack]"))
		vim.command("setlocal buftype=nofile nowrap noautoindent nobuflisted tw=0")
		VDBStackWindow = vim.current.window
		vim.command("autocmd BufLeave <buffer> python VDBStackWindow = VDBWindowDeleted(VDBStackWindow)")
		vim.command("nnoremap <buffer> <silent> <CR> :python VDBJumpToStackFrame()<CR>")
		VDBFindWindow(win)
		VDBUpdateWatches()
	else:
		if VDBFindWindow(VDBStackWindow):
			vim.command("wincmd c")
			VDBStackWindow = None

def VDBJumpToStackFrame():
	frame = VDBStack[vim.current.window.cursor[0]-1]
	VDBFindWindow(VDBSourceWindow)
	if VDBSourceWindow.buffer.name != frame[0]:
		vim.command("silent edit %s"%(frame[0]))
	vim.command("%i"%(frame[1]))

def VDBUntil():
	global VDBSession
	global VDBExecFilename
	
	bufname = vim.current.buffer.name
	lineno = vim.current.window.cursor[0]
	if VDBSession is None:
		VDBInitSession()
		VDBSession.process()
		VDBFindWindow(VDBSourceWindow)
	if vim.current.window != VDBSourceWindow:
		return
	if VDBSession.state != READY:
		return
	VDBSession.writeline("break %s:%i"%(bufname, lineno))
	VDBSession.process()
	VDBSession.writeline("continue")
	VDBSession.process()
	VDBSession.writeline("clear %s:%i"%(bufname, lineno))
	if VDBSession.process():
		VDBKill()
		return
	VDBUpdateWatches()

def VDBToggleBreak():
	global VDBSession
	global VDBBreakpoint

	key = "%s:%i"%(vim.current.buffer.name, vim.current.window.cursor[0])
	if VDBBreakpoint.has_key(key):
		if VDBSession is not None:
			VDBSession.writeline("clear %s"%(key))
			VDBSession.process()
		vim.command("sign unplace %i file=%s"%(VDBBreakpoint[key].signnum,VDBBreakpoint[key].buffer))
		del VDBBreakpoint[key]
	else:
		if VDBSession is not None:
			VDBSession.writeline("break %s"%(key))
			VDBSession.process()
			if VDBSession.breaknum is None:
				print "Could not set break point"
				return
			VDBBreakpoint[key] = TBreakpoint(len(VDBBreakpoint)+1,vim.current.buffer.name,VDBSession.breaknum)
		else:
			VDBBreakpoint[key] = TBreakpoint(len(VDBBreakpoint)+1,vim.current.buffer.name,-1)

		vim.command("sign place %i line=%i name=BreakPoint file=%s"%(len(VDBBreakpoint),vim.current.window.cursor[0],vim.current.buffer.name))

def VDBBreakpointCondition():
	global VDBSession
	global VDBBreakpoint

	key = "%s:%i"%(vim.current.buffer.name, vim.current.window.cursor[0])
	if VDBBreakpoint.has_key(key):
		VDBBreakpoint[key].condition = VDBGetInput("Condition: ", VDBBreakpoint[key].condition, None)
		if VDBSession is not None:
			VDBSession.writeline("condition %i %s"%(VDBBreakpoint[key].number,VDBBreakpoint[key].condition))
			VDBSession.process()
		vim.command("sign unplace %i file=%s"%(VDBBreakpoint[key].signnum,VDBBreakpoint[key].buffer))
		if VDBBreakpoint[key].condition == "":
			vim.command("sign place %i line=%i name=BreakPoint file=%s"%(VDBBreakpoint[key].signnum,vim.current.window.cursor[0],VDBBreakpoint[key].buffer))
		else:
			vim.command("sign place %i line=%i name=CondBreakPoint file=%s"%(VDBBreakpoint[key].signnum,vim.current.window.cursor[0],VDBBreakpoint[key].buffer))
	else:
		print "No breakpoint to set condition for"

def VDBAddWatch():
	global VDBWatches

	if vim.current.window == VDBWatchWindow: #buffer.name.endswith("[VDB %i: Watches]"%(os.getpid())):
		newwatch = VDBGetInput("New watch: ", vim.current.buffer[vim.current.window.cursor[0]-1].split(":")[0], None)
		VDBWatches[vim.current.window.cursor[0]-1] = newwatch
	else:
		newwatch = VDBGetInput("New watch: ", "", None)
		VDBWatches.append(newwatch)
	VDBUpdateWatches()

def VDBMoveWatchUp():
	global VDBWatches

	if vim.current.window == VDBWatchWindow: #buffer.name.endswith("[VDB %i: Watches]"%(os.getpid())):
		line = vim.current.window.cursor[0]-1
		if line > 0:
			temp = vim.current.buffer[line-1]
			vim.current.buffer[line-1] = vim.current.buffer[line]
			vim.current.buffer[line] = temp
			vim.command("normal k")

def VDBMoveWatchDown():
	global VDBWatches

	if vim.current.window == VDBWatchWindow: #buffer.name.endswith("[VDB %i: Watches]"%(os.getpid())):
		line = vim.current.window.cursor[0]-1
		if line < len(VDBWatches)-1:
			temp = vim.current.buffer[line+1]
			vim.current.buffer[line+1] = vim.current.buffer[line]
			vim.current.buffer[line] = temp
			vim.command("normal j")

def VDBDelWatch():
	global VDBWatches

	if vim.current.window == VDBWatchWindow: #buffer.name.endswith("[VDB %i: Watches]"%(os.getpid())):
		del VDBWatches[vim.current.window.cursor[0]-1]
		VDBUpdateWatches()

def VDBStepInto():
	global VDBSession

	if VDBSession is None:
		VDBInitSession()
	else:
		if vim.current.window != VDBSourceWindow:
			return
		if VDBSession.state != READY:
			return
		VDBSession.writeline("step")
	if VDBSession.process():
		VDBKill()
		return
	VDBUpdateWatches()

def VDBStepOver():
	global VDBSession

	if VDBSession is None:
		VDBInitSession()
	else:
		if vim.current.window != VDBSourceWindow:
			return
		if VDBSession.state != READY:
			return
		VDBSession.writeline("next")
	if VDBSession.process():
		VDBKill()
		return
	VDBUpdateWatches()

def VDBFinish():
	global VDBSession

	if VDBInitSession():
		if vim.current.window != VDBSourceWindow:
			return
		if VDBSession.state != READY:
			return
		VDBSession.writeline("return")
	if VDBSession.process():
		VDBKill()
		return
	VDBUpdateWatches()

def VDBContinue():
	global VDBSession

	if VDBSession is None:
		VDBInitSession()
	else:
		if vim.current.window != VDBSourceWindow:
			return
		if VDBSession.state != READY:
			return
		VDBSession.writeline("continue")
	if VDBSession.process():
		VDBKill()
		return
	VDBUpdateWatches()

def VDBKill():
	global VDBStackWindow
	global VDBConsoleWindow
	global VDBWatchWindow
	global VDBWatchBuffer
	global VDBOutputWindow
	global VDBOutputBuffer
	global VDBSession
	global VDBExecFilename
	global VDBStack

	if VDBSession is not None:
		if VDBFindWindow(VDBStackWindow):
			vim.command("bdelete")
		if VDBFindWindow(VDBConsoleWindow):
			vim.command("bdelete")
		if VDBFindWindow(VDBWatchWindow):
			vim.command("bdelete")
		elif VDBWatchBuffer is not None:
			vim.command("silent! bdelete %i"%(VDBWatchBuffer.number))
		if VDBFindWindow(VDBOutputWindow):
			vim.command("bdelete")
		elif VDBOutputBuffer is not None:
			vim.command("silent! bdelete %i"%(VDBOutputBuffer.number))
		VDBStackWindow = None
		VDBConsoleWindow = None
		VDBWatchWindow = None
		VDBWatchBuffer = None
		VDBOutputWindow = None
		VDBOutputBuffer = None
	
		VDBFindWindow(VDBSourceWindow)
		for b in VDBSession.unmodifiablebuffers:
			vim.command("silent! buffer %i"%(b))
			vim.command("setlocal modifiable")
		try:
			VDBSession = None
		except IOError:
			pass
		VDBStack = []
		if VDBExecFilename is not None:
			vim.command("sign unplace 65535 file=%s"%(VDBExecFilename))
			VDBExecFilename = None
