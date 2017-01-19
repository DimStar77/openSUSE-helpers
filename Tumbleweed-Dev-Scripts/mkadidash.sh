
echo '
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
<title>Interim adi dashboard - just to get some start</title>
<link rel="stylesheet" media="all" href="application.css" />
<link href="https://maxcdn.bootstrapcdn.com/font-awesome/4.1.0/css/font-awesome.min.css" rel="stylesheet">
</head>

<body>

<table class="dashboard" id="staging-dashboard">

<tr> <td>number</td><td>packages</td><td>status assessment</td> </tr>'

for adi in $(osc api "/search/project/id?match=starts-with(@name,'openSUSE:Factory:Staging:adi:')" | grep -o "[0-9]\+" | sort -n ); do
  echo "Checking adi project ${adi}" >&2

  echo "<tr><td><a href='https://build.opensuse.org/project/staging_projects/openSUSE:Factory/adi:$adi'>adi:$adi</a></td><td><ul class="packages-list">"
# Get all requests in a staging project
  for rq in $(osc review list -P openSUSE:Factory:Staging:adi:$adi | awk '/State:review/ {print $1}'); do
    STATUS="ok request"
    ICON=""
    RQXML=""
# Get package name from a submission
    PKG=$(osc rq show $rq |  awk '/submit:.*\/.*@.*->.*/ {print $2}' | grep -o "\/.*@" | tr -d /@)
    RQXML=$(osc api /request/$rq )
    if [ $(echo "$RQXML"  | grep 'review state="new"' -c) -gt 1 ]; then
	    STATUS="review request"
    fi
    if [ "$STATUS" = "review request" ]; then
	    # Check which reviews are pending and add the correspoding icons - we have the XML in memory
	    echo "$RQXML" | grep '<review state="new" by_group="opensuse-review-team">' > /dev/null && ICON="$ICON <i class='fa fa-search'></i>"
	    echo "$RQXML" | grep '<review state="new" by_group="legal-team">' > /dev/null && ICON="$ICON <i class='fa fa-graduation-cap'></i>"
	    echo "$RQXML" | grep '<review state="new".*by_user="factory-repo-checker">' > /dev/null && ICON="$ICON <i class='fa fa-cog'></i>"
    fi

    echo "<li class='${STATUS}'><a href='https://build.opensuse.org/request/show/$rq'>$PKG ${ICON}</a></li>"

  done

  echo "</ul></td></tr>"
done

echo '</table></body>'

