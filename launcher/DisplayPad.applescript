-- Opens the DisplayPad configurator in the browser (built by install.sh).
property port : "8765"

on serviceUp()
	try
		do shell script "/usr/bin/curl -s -o /dev/null -m 1 http://127.0.0.1:" & port & "/"
		return true
	on error
		return false
	end try
end serviceUp

-- "en" or "es", from the "language" setting in config.json
on uiLanguage(appDir)
	try
		return do shell script "/usr/bin/plutil -extract language raw -o - " & quoted form of (appDir & "config.json")
	on error
		return "en"
	end try
end uiLanguage

on run
	set appDir to (POSIX path of (path to home folder)) & "Library/Application Support/DisplayPadMac/"
	set isSpanish to (uiLanguage(appDir) is "es")
	if not serviceUp() then
		do shell script "launchctl kickstart gui/$(id -u)/com.displaypadmac.agent >/dev/null 2>&1; true"
		repeat 20 times
			delay 0.5
			if serviceUp() then exit repeat
		end repeat
	end if
	if not serviceUp() then
		if isSpanish then
			set msg to "El servicio no responde. Revisa el registro en ~/Library/Logs/DisplayPadMac.log o vuelve a ejecutar install.sh."
		else
			set msg to "The service is not responding. Check the log in ~/Library/Logs/DisplayPadMac.log or run install.sh again."
		end if
		display alert "DisplayPad" message msg as critical
		return
	end if
	try
		set tok to do shell script "cat " & quoted form of (appDir & ".token")
	on error
		if isSpanish then
			set msg to "No se encontró la clave del configurador. Vuelve a ejecutar install.sh."
		else
			set msg to "The configurator key was not found. Run install.sh again."
		end if
		display alert "DisplayPad" message msg as critical
		return
	end try
	do shell script "open " & quoted form of ("http://127.0.0.1:" & port & "/#t=" & tok)
end run
